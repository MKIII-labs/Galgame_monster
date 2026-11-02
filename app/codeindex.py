# -*- coding: utf-8 -*-
"""代码库语义检索（本地 embedding，不消耗 LLM token）。

- 只索引沙箱内的文本文件；按行切块（带重叠），把「相对路径 + 代码」一起做 embedding，
  这样查「工具组怎么折叠」也能命中 `chat.js`。
- 模型：fastembed + BAAI/bge-small-zh-v1.5（本地 ONNX，512 维）。首次使用前需要
  运行 `python scripts/fetch_embed_model.py` 下载（约 95MB）；模型缺失时自动降级为
  关键词检索，并在结果里注明，工具不会因此报错。
- 索引按「沙箱根 + 文件签名」持久化到 `data/codeindex/`；文件改动后签名变化会自动重建。
"""
import fnmatch
import hashlib
import json
import math
import os
import re
import threading
import time

import numpy as np

from app.store import DATA_DIR

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_CACHE = os.path.join(DATA_DIR, "models", "fastembed")
INDEX_DIR = os.path.join(DATA_DIR, "codeindex")
INDEX_VERSION = 2

CHUNK_LINES = 80           # 每块多少行（符号边界会进一步切开）
CHUNK_OVERLAP = 10         # 相邻块重叠行数
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 0              # 0 = 不限；可在设置里用 codeindex_max_files 覆盖
MAX_CHUNKS = 0             # 0 = 不限
MAX_TOTAL_BYTES = 0
TOP_K = 6
MIN_SCORE = 0.34
RESULT_CHARS = 7000

# 依赖目录 / 构建产物不参与索引；项目自己的 .gitignore 也会被读取
IGNORE_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
               ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
               "target", "out", ".next", ".nuxt", ".cache", ".idea", ".vs"}


CODE_EXT = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".css", ".scss", ".sass", ".less", ".html", ".htm", ".json", ".md", ".txt",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".ps1", ".bat",
    ".sql", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".go", ".rs",
    ".php", ".rb", ".lua", ".xml", ".csv", ".env",
}
ALLOW_NO_EXT = {"Makefile", "makefile", "Dockerfile", "Rakefile", "Gemfile",
                "Procfile", "LICENSE", "README"}

_LOCK = threading.Lock()
_CACHE = {}                 # root -> {"sig": str, "meta": [...], "vec": np.ndarray}
_EMBED_CACHE = {"model": None, "checked": 0.0, "ok": False}

# 代码/中文混合分词：标识符按驼峰与下划线拆开，中文按「字 + 二元组」
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[\u4e00-\u9fff]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _tokens(text):
    out = []
    for m in _WORD_RE.finditer(text or ""):
        w = m.group(0)
        if w[0].isascii():
            for part in w.split("_"):
                out.extend(_CAMEL_RE.findall(part))
        else:
            out.extend(w)
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
    return [t.lower() for t in out if t]


def _bm25_index(meta):
    docs, df = [], {}
    for m in meta:
        toks = _tokens((m.get("path") or "") + "\n" + (m.get("text") or ""))
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        docs.append((tf, len(toks)))
        for t in tf:
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    avg = (sum(ln for _tf, ln in docs) / n) if n else 0.0
    return {"docs": docs, "df": df, "n": n, "avg": avg or 1.0}


def _bm25_scores(bm, query):
    qs = set(_tokens(query))
    if not qs or not bm["n"]:
        return [0.0] * bm["n"]
    out = [0.0] * bm["n"]
    k1, b = 1.2, 0.75
    n = bm["n"]
    for i, (tf, dl) in enumerate(bm["docs"]):
        s = 0.0
        for w in qs:
            f = tf.get(w)
            if not f:
                continue
            idf = math.log(1.0 + (n - bm["df"].get(w, 0) + 0.5)
                           / (bm["df"].get(w, 0) + 0.5))
            s += idf * f * (k1 + 1.0) / (f + k1 * (1.0 - b + b * dl / bm["avg"]))
        out[i] = s
    return out


def _identifier_query(query):
    """纯标识符查询（如 setToolGroupOpen）→ 关键词检索更可信，加权更高。"""
    return bool(re.fullmatch(r"[\sA-Za-z0-9_.$/-]+", query or ""))


class Unavailable(Exception):
    """语义模型不可用（未下载 / 依赖缺失 / 加载失败）。"""


# ---------------------------------------------------------------- embedding
def model_ready():
    """模型文件是否已存在（不触发下载）。结果缓存 60 秒。"""
    now = time.time()
    if now - _EMBED_CACHE["checked"] < 60:
        return _EMBED_CACHE["ok"]
    ok = False
    try:
        for dirpath, _dirs, files in os.walk(MODEL_CACHE):
            if any(f.endswith(".onnx") for f in files):
                ok = True
                break
    except OSError:
        ok = False
    _EMBED_CACHE["checked"] = now
    _EMBED_CACHE["ok"] = ok
    return ok


def _embedder():
    if _EMBED_CACHE["model"] is not None:
        return _EMBED_CACHE["model"]
    if not model_ready():
        raise Unavailable("语义模型未下载")
    try:
        from fastembed import TextEmbedding
    except Exception as e:                       # 依赖缺失
        raise Unavailable("fastembed 不可用：%s" % e)
    try:
        m = TextEmbedding(model_name=MODEL_NAME, cache_dir=MODEL_CACHE)
    except Exception as e:
        raise Unavailable("加载语义模型失败：%s" % e)
    _EMBED_CACHE["model"] = m
    return m


def _embed_texts(texts, batch=32):
    m = _embedder()
    out = []
    for i in range(0, len(texts), batch):
        part = list(m.embed(texts[i:i + batch]))
        out.extend(part)
    return np.asarray(out, dtype=np.float32)


def embed_texts(texts):
    """对外暴露的 embedding 入口：模型不可用 / 依赖缺失时返回 None。

    记忆检索（knowledge._rank_items）复用它做向量召回；拿不到就回退关键词，
    不能因为没装 fastembed 让整轮对话失败。
    """
    texts = [str(t or "") for t in (texts or [])]
    if not texts:
        return None
    try:
        return _embed_texts(texts)
    except Unavailable:
        return None
    except Exception as e:                 # noqa: BLE001
        print("[向量] embedding 失败：%s" % e)
        return None


# ---------------------------------------------------------------- 文件 / 分块
def _looks_binary(path, probe=4096):
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(probe)
    except OSError:
        return True


def _limits():
    """索引预算：设置里可覆盖；0 = 不限（全仓）。"""
    try:
        from app.config import load_config
        c = load_config() or {}
    except Exception:                         # noqa: BLE001
        c = {}

    def _int(key, default):
        try:
            return int(c.get(key, default) or 0)
        except (TypeError, ValueError):
            return default
    return {
        "enabled": bool(c.get("codeindex_enabled", True)),
        "max_files": _int("codeindex_max_files", MAX_FILES),
        "max_chunks": _int("codeindex_max_chunks", MAX_CHUNKS),
        "max_file_bytes": _int("codeindex_max_file_kb", MAX_FILE_BYTES // 1024) * 1024,
    }


def _gitignore_patterns(root):
    p = os.path.join(root, ".gitignore")
    out = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                out.append(line.lstrip("/"))
    except OSError:
        pass
    return out


def _ignored(rel, patterns):
    if not patterns:
        return False
    base = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        p = pat.rstrip("/")
        if pat.endswith("/") and (rel == p or rel.startswith(p + "/")):
            return True
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(base, p):
            return True
        if rel.startswith(p + "/"):
            return True
    return False


def _scan(root):
    """扫描沙箱：返回 [(abs, rel, size, mtime_ns)]，遵守 .gitignore 与依赖目录。"""
    lim = _limits()
    if not lim["enabled"]:
        return []
    pats = _gitignore_patterns(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in IGNORE_DIRS and not d.startswith(".git")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext and ext not in CODE_EXT:
                continue
            if not ext and fn not in ALLOW_NO_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if _ignored(rel, pats):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size <= 0 or st.st_size > lim["max_file_bytes"]:
                continue
            out.append((full, rel, st.st_size, st.st_mtime_ns))
    out.sort(key=lambda x: x[2])
    if lim["max_files"] > 0:
        out = out[:lim["max_files"]]
    return out


def _collect_files(root):
    """兼容旧调用：返回 [(abs, rel, size)]。"""
    return [(full, rel, size) for full, rel, size, _m in _scan(root)]


def _signature(files):
    h = hashlib.sha1()
    h.update(("%s|%s|%s|%s" % (INDEX_VERSION, CHUNK_LINES, CHUNK_OVERLAP,
                               _limits()["max_file_bytes"])).encode())
    for item in files:
        rel, size = item[1], item[2]
        mtime = item[3] if len(item) > 3 else 0
        h.update(("%s|%d|%d\n" % (rel, size, mtime)).encode("utf-8", "ignore"))
    return h.hexdigest()


# 符号边界：跨语言的常见定义写法；只用于「把长文件切成语义块」，不做精确解析
_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|fn|func|struct|interface"
    r"|trait|enum|type)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:(?:public|private|protected|internal|static|final|abstract|synchronized"
    r"|native|virtual|override|async|const|extern|inline)\s+)+"
    r"[A-Za-z_][\w:<>\[\],.?*&]*\s+([A-Za-z_$][\w$]*)\s*\("
    r"|^\s*(?:public|private|protected|internal)\s+"
    r"(?:class|interface|enum|struct)\s+([A-Za-z_$][\w$]*)")


def _symbol_of(line):
    m = _SYMBOL_RE.match(line or "")
    if not m:
        return ""
    return next((g for g in m.groups() if g), "")


def _chunk_file(full, rel):
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    if _looks_binary(full):
        return []
    # 先按顶层定义切段，再按行数切块：比固定 40 行更贴近「一个函数一块」
    bounds, start = [], 0
    for i, line in enumerate(lines):
        if i > start + 8 and _SYMBOL_RE.match(line):
            bounds.append((start, i))
            start = i
    if start < len(lines):
        bounds.append((start, len(lines)))

    out = []
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    for s, e in bounds:
        for cstart in range(s, max(s + 1, e), step):
            cend = min(e, cstart + CHUNK_LINES)
            text = "\n".join(lines[cstart:cend]).strip()
            if text:
                symbol = _symbol_of(lines[cstart] if cstart < len(lines) else "")
                emb_text = "# %s%s\n%s" % (rel, (":" + symbol) if symbol else "", text)
                out.append({"path": rel, "start": cstart + 1, "end": cend,
                            "text": text, "symbol": symbol, "emb": emb_text})
            if cend >= e:
                break
    return out


# ---------------------------------------------------------------- 索引构建 / 持久化
def _cache_path(root):
    key = hashlib.sha1(root.lower().encode("utf-8", "ignore")).hexdigest()[:16]
    return os.path.join(INDEX_DIR, key + ".npz")


def _load_disk(root, sig=None):
    path = _cache_path(root)
    if not os.path.isfile(path):
        return None
    try:
        data = np.load(path, allow_pickle=False)
        if "version" in data and int(data["version"]) != INDEX_VERSION:
            return None
        if sig is not None and str(data["sig"]) != sig:
            return None
        meta = json.loads(str(data["meta"]))
        vec = data["vec"]
        files = json.loads(str(data["files"])) if "files" in data else {}
        if len(meta) != len(vec):
            return None
        return {"sig": str(data["sig"]), "meta": meta, "vec": vec, "files": files}
    except Exception:                         # noqa: BLE001
        return None


def _save_disk(root, idx):
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        path = _cache_path(root)
        tmp = path + ".tmp.npz"
        np.savez_compressed(tmp, version=np.array(INDEX_VERSION),
                            sig=np.array(idx["sig"]),
                            meta=np.array(json.dumps(idx["meta"], ensure_ascii=False)),
                            files=np.array(json.dumps(idx.get("files") or {},
                                                      ensure_ascii=False)),
                            vec=idx["vec"])
        os.replace(tmp, path)
    except Exception:                         # noqa: BLE001
        pass                                  # 缓存失败不影响本次检索


def _empty_vec():
    return np.zeros((0, 1), dtype=np.float32)


def _chunks_for(root, files, max_chunks, progress=None):
    chunks, indexed = [], 0
    for i, (full, rel, _size, _mtime) in enumerate(files):
        got = _chunk_file(full, rel)
        if got:
            indexed += 1
            chunks.extend(got)
        if max_chunks and len(chunks) >= max_chunks:
            chunks = chunks[:max_chunks]
            break
        if progress:
            progress(i + 1, len(files), len(chunks))
    return chunks, indexed


def _build(root, files, sig, progress=None):
    lim = _limits()
    chunks, indexed = _chunks_for(root, files, lim["max_chunks"], progress)
    manifest = {f[1]: [f[2], f[3]] for f in files}
    if not chunks:
        return {"sig": sig, "meta": [], "vec": _empty_vec(), "files": manifest,
                "indexed": 0}
    vec = _embed_texts([c.pop("emb") for c in chunks])
    return {"sig": sig, "meta": chunks, "vec": vec, "files": manifest,
            "indexed": indexed}


def _incremental(root, files, sig, old, progress=None):
    """只重算变更文件；未变文件的向量直接复用。"""
    lim = _limits()
    old_files = old.get("files") or {}
    cur = {f[1]: [f[2], f[3]] for f in files}
    changed = {rel for rel, meta in cur.items()
               if old_files.get(rel) != list(meta)}
    removed = set(old_files) - set(cur)
    if not changed and not removed:
        old["sig"] = sig
        return old
    keep = [i for i, m in enumerate(old.get("meta") or [])
            if m.get("path") not in changed and m.get("path") not in removed]
    keep_meta = [old["meta"][i] for i in keep]
    keep_vec = old["vec"][keep] if keep else _empty_vec()
    new_files = [f for f in files if f[1] in changed]
    new_chunks, _ = _chunks_for(root, new_files, lim["max_chunks"], progress)
    if not new_chunks:
        meta, vec = keep_meta, keep_vec
    else:
        new_vec = _embed_texts([c.pop("emb") for c in new_chunks])
        if keep_vec.size and keep_vec.shape[1] != new_vec.shape[1]:
            return _build(root, files, sig, progress)   # 维度变了：整库重建
        meta = keep_meta + new_chunks
        vec = np.vstack([keep_vec, new_vec]) if keep_vec.size else new_vec
    return {"sig": sig, "meta": meta, "vec": vec, "files": cur,
            "indexed": len({m.get("path") for m in meta})}


def get_index(root, force=False, progress=None):
    """取（必要时增量构建）索引。返回 (idx, files_total, files_indexed)。"""
    files = _scan(root)
    sig = _signature(files)
    cached = _CACHE.get(root)
    if not force and cached and cached["sig"] == sig:
        return cached, len(files), cached.get("indexed", len(files))
    with _LOCK:
        cached = _CACHE.get(root)
        if not force and cached and cached["sig"] == sig:
            return cached, len(files), cached.get("indexed", len(files))
        old = None if force else _load_disk(root)
        if old and old.get("files"):
            idx = _incremental(root, files, sig, old, progress)
        else:
            idx = _build(root, files, sig, progress)
        _CACHE[root] = idx
        _save_disk(root, idx)
        return idx, len(files), idx.get("indexed", len(files))


# ---------------------------------------------------------------- 后台索引 / 状态
_JOBS = {}
_JOB_LOCK = threading.Lock()


def status(root):
    """索引状态：idle / running / ready / error。"""
    with _JOB_LOCK:
        job = _JOBS.get(root)
        if job and job.get("state") == "running":
            return dict(job)
    idx = _CACHE.get(root)
    if idx:
        return {"state": "ready", "files": len(idx.get("files") or {}),
                "chunks": len(idx.get("meta") or []),
                "indexed": idx.get("indexed", 0), "error": ""}
    if job and job.get("state") == "error":
        return dict(job)
    return {"state": "idle", "files": 0, "chunks": 0, "indexed": 0, "error": ""}


def _run_job(root):
    def progress(done, total, chunks):
        with _JOB_LOCK:
            job = _JOBS.get(root)
            if job is not None:
                job.update({"done": done, "total": total, "chunks": chunks})
    try:
        idx, total, indexed = get_index(root, force=True, progress=progress)
        with _JOB_LOCK:
            _JOBS[root] = {"state": "ready", "files": total,
                           "chunks": len(idx.get("meta") or []),
                           "indexed": indexed, "error": "", "done": total,
                           "total": total}
    except Exception as e:                    # noqa: BLE001
        with _JOB_LOCK:
            _JOBS[root] = {"state": "error", "files": 0, "chunks": 0,
                           "indexed": 0, "error": str(e), "done": 0, "total": 0}


def reindex(root, background=True):
    """重建索引；background=True 时立刻返回 running 状态。"""
    with _JOB_LOCK:
        cur = _JOBS.get(root)
        if cur and cur.get("state") == "running":
            return dict(cur)
        _JOBS[root] = {"state": "running", "files": 0, "chunks": 0,
                       "indexed": 0, "error": "", "done": 0, "total": 0}
    if not background:
        _run_job(root)
        return status(root)
    threading.Thread(target=_run_job, args=(root,), daemon=True).start()
    return status(root)



# ---------------------------------------------------------------- 检索
def _snippet(meta, max_lines=34):
    lines = (meta.get("text") or "").splitlines()
    if len(lines) <= max_lines:
        body = lines
    else:
        body = lines[:max_lines]
    numbered = "\n".join("%5d  %s" % (meta["start"] + i, ln) for i, ln in enumerate(body))
    if len(body) < len(lines):
        numbered += "\n      …（本段还有 %d 行，需要时可 read_file 精读）" % (len(lines) - len(body))
    return numbered


def search(sandbox, query, limit=TOP_K, boost=None):
    """返回给模型看的检索结果文本。语义模型不可用时自动退回关键词检索。

    boost：{相对路径: 加权}。符号图（app/codesym.py）找到定义 / 引用的文件会传进来，
    让「定义所在文件」排在「只是字面提到」的前面；不传则行为与原来完全一致。
    """
    from app import agent                        # 延迟导入，避免循环依赖
    query = str(query or "").strip()
    if not query:
        return "请提供 query：用一句自然语言描述你要找的代码或逻辑。"
    try:
        root = agent._real(agent._sandbox_root(sandbox))
    except Exception as e:
        return "沙箱不可用：%s" % e
    try:
        limit = max(1, min(int(limit or TOP_K), 10))
    except (TypeError, ValueError):
        limit = TOP_K

    t0 = time.time()
    job = status(root)
    if job.get("state") == "running" and not _CACHE.get(root):
        return ("代码库索引正在后台构建（%s/%s 文件，%s 块）。完成后可重试，"
                "或先用 search_files 按关键字查找。"
                % (job.get("done") or 0, job.get("total") or "?", job.get("chunks") or 0))
    try:
        idx, total, _ = get_index(root)
    except Unavailable as e:
        return _keyword_fallback(root, query, limit, str(e))
    if not idx["meta"]:
        return "沙箱里没有可索引的文本文件（或都被大小 / 类型过滤了）。"

    try:
        qv = _embed_texts([query])[0]
    except Unavailable as e:
        return _keyword_fallback(root, query, limit, str(e))
    vec = idx["vec"]
    dense = vec @ qv                            # fastembed 输出已归一化
    bm = idx.get("bm25")
    if bm is None:
        bm = _bm25_index(idx["meta"])
        idx["bm25"] = bm
    sparse = _bm25_scores(bm, query)
    n = len(idx["meta"])

    # RRF 融合：稠密检索管「概念」，BM25 管「标识符 / 字面量」；
    # 纯标识符查询（setToolGroupOpen）给 BM25 更高权重。
    cand = min(n, max(limit * 10, 60))
    d_order = [int(i) for i in np.argsort(-dense)[:cand]]
    s_order = sorted(range(n), key=lambda i: -sparse[i])[:cand]
    w_sparse = 1.6 if _identifier_query(query) else 1.0
    fused = {}
    for r, i in enumerate(d_order):
        fused[i] = fused.get(i, 0.0) + 1.0 / (60 + r + 1)
    for r, i in enumerate(s_order):
        fused[i] = fused.get(i, 0.0) + w_sparse / (60 + r + 1)

    q_tokens = set(_tokens(query))
    path_hits = {}
    for i in list(fused):
        path = (idx["meta"][i].get("path") or "").lower()
        hits = sum(1 for t in q_tokens if len(t) >= 2 and t in path)
        path_hits[i] = hits
        if hits:
            fused[i] += 0.02 * min(hits, 3)

    # 符号图加权：有精确定义的文件 > 只是文本里出现过
    if boost:
        low = {str(k).replace("\\", "/").lower(): float(v) for k, v in boost.items()}
        for i in list(fused):
            w = low.get((idx["meta"][i].get("path") or "").lower())
            if w:
                fused[i] += w
    # 精确符号命中：查 setToolGroupOpen 时，定义它的块排在前面
    for i in list(fused):
        sym = str(idx["meta"][i].get("symbol") or "").lower()
        if sym and sym in q_tokens:
            fused[i] += 0.08

    picked, seen = [], {}
    for i in sorted(fused, key=lambda k: -fused[k]):
        meta = idx["meta"][i]
        path = meta["path"]
        if agent.is_secret_path(path):           # 密钥 / 凭据文件不进检索结果
            continue
        if seen.get(path, 0) >= 2:               # 同一文件最多 2 段
            continue
        if not (float(dense[i]) >= MIN_SCORE or sparse[i] > 0 or path_hits.get(i)):
            continue                             # 两路都没命中：丢掉
        seen[path] = seen.get(path, 0) + 1
        picked.append((float(dense[i]), bool(sparse[i] > 0), meta))
        if len(picked) >= limit:
            break
    if not picked:
        return ("代码检索没有找到足够相关的内容（索引 %d 段 / %d 个文件）。"
                "可以换一种说法，或改用 search_files 按关键字查找。"
                % (len(idx["meta"]), total))

    used, out = 0, []
    head = ("代码检索「%s」命中 %d 段（向量 + 关键词，索引 %d 段 / %d 个文件，%.1fs）："
            % (query, len(picked), len(idx["meta"]), total, time.time() - t0))
    out.append(head)
    for score, kw, meta in picked:
        tag = "相似度 %.2f" % score
        if kw:
            tag += " · 关键词命中"
        block = ("--- %s:%d-%d  (%s)\n%s"
                 % (meta["path"], meta["start"], meta["end"], tag, _snippet(meta)))
        if used + len(block) > RESULT_CHARS:
            break
        used += len(block)
        out.append(block)
    return "\n\n".join(out)


def search_items(sandbox, query, limit=20, boost=None):
    """结构化检索结果（IDE 语义搜索用）：[{path,start,end,symbol,score,snippet}]。"""
    from app import agent
    query = str(query or "").strip()
    if not query:
        return {"items": [], "error": "请提供 query"}
    try:
        root = agent._real(agent._sandbox_root(sandbox))
    except Exception as e:                    # noqa: BLE001
        return {"items": [], "error": "沙箱不可用：%s" % e}
    job = status(root)
    if job.get("state") == "running" and not _CACHE.get(root):
        return {"items": [], "building": True, "status": job}
    try:
        idx, total, indexed = get_index(root)
        qv = _embed_texts([query])[0]
    except Unavailable as e:
        return {"items": [], "error": str(e), "fallback": True}
    if not idx["meta"]:
        return {"items": [], "total": total, "indexed": indexed}
    dense = idx["vec"] @ qv
    bm = idx.get("bm25")
    if bm is None:
        bm = _bm25_index(idx["meta"])
        idx["bm25"] = bm
    sparse = _bm25_scores(bm, query)
    n = len(idx["meta"])
    cand = min(n, max(limit * 10, 60))
    w_sparse = 1.6 if _identifier_query(query) else 1.0
    fused = {}
    for r, i in enumerate(np.argsort(-dense)[:cand]):
        fused[int(i)] = fused.get(int(i), 0.0) + 1.0 / (60 + r + 1)
    for r, i in enumerate(sorted(range(n), key=lambda k: -sparse[k])[:cand]):
        fused[i] = fused.get(i, 0.0) + w_sparse / (60 + r + 1)
    q_tokens = set(_tokens(query))
    if boost:
        low = {str(k).replace("\\", "/").lower(): float(v) for k, v in boost.items()}
        for i in list(fused):
            w = low.get((idx["meta"][i].get("path") or "").lower())
            if w:
                fused[i] += w
    for i in list(fused):
        sym = str(idx["meta"][i].get("symbol") or "").lower()
        if sym and sym in q_tokens:
            fused[i] += 0.08
    picked, seen = [], {}
    for i in sorted(fused, key=lambda k: -fused[k]):
        meta = idx["meta"][i]
        path = meta["path"]
        if agent.is_secret_path(path) or seen.get(path, 0) >= 2:
            continue
        seen[path] = seen.get(path, 0) + 1
        lines = (meta.get("text") or "").splitlines()[:12]
        snippet = "\n".join(lines)
        picked.append({"path": path, "start": meta["start"], "end": meta["end"],
                       "symbol": meta.get("symbol") or "",
                       "score": round(float(dense[i]), 3),
                       "keyword": bool(sparse[i] > 0), "snippet": snippet})
        if len(picked) >= limit:
            break
    return {"items": picked, "total": total, "indexed": indexed,
            "chunks": len(idx["meta"])}


def _keyword_fallback(root, query, limit, why):
    """模型没就绪时的兜底：按空白拆词，逐行做包含匹配（不消耗 token）。"""
    words = [w for w in re.split(r"\s+", query) if w][:6]
    if not words:
        return "语义模型未就绪（%s），且查询没有可用关键词。" % why
    hits = []
    from app import agent                        # 延迟导入，避免循环依赖
    for full, rel, _size in _collect_files(root):
        if agent.is_secret_path(rel):            # 密钥 / 凭据文件不参与兜底检索
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines):
            low = ln.lower()
            score = sum(1 for w in words if w.lower() in low)
            if score:
                ctx = "\n".join("%5d  %s" % (i + j + 1, lines[i + j])
                                for j in range(0, min(3, len(lines) - i)))
                hits.append((score, rel, i + 1, ctx))
    hits.sort(key=lambda x: (-x[0], x[1], x[2]))
    if not hits:
        return ("语义模型未就绪（%s），关键词检索也没有命中。"
                "可运行 `python scripts/fetch_embed_model.py` 启用语义检索。" % why)
    out = ["（语义模型未就绪：%s；已退回关键词检索，命中 %d 处）" % (why, len(hits))]
    for _score, rel, line, ctx in hits[:limit]:
        out.append("--- %s:%d\n%s" % (rel, line, ctx))
    return "\n\n".join(out)
