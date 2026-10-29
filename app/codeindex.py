# -*- coding: utf-8 -*-
"""代码库语义检索（本地 embedding，不消耗 LLM token）。

- 只索引沙箱内的文本文件；按行切块（带重叠），把「相对路径 + 代码」一起做 embedding，
  这样查「工具组怎么折叠」也能命中 `chat.js`。
- 模型：fastembed + BAAI/bge-small-zh-v1.5（本地 ONNX，512 维）。首次使用前需要
  运行 `python scripts/fetch_embed_model.py` 下载（约 95MB）；模型缺失时自动降级为
  关键词检索，并在结果里注明，工具不会因此报错。
- 索引按「沙箱根 + 文件签名」持久化到 `data/codeindex/`；文件改动后签名变化会自动重建。
"""
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
INDEX_VERSION = 1

CHUNK_LINES = 40           # 每块多少行
CHUNK_OVERLAP = 8          # 相邻块重叠行数
MAX_FILE_BYTES = 200 * 1024
MAX_FILES = 1200           # 超过就只索引较小的文件，避免首次建索引过慢
MAX_CHUNKS = 1500
MAX_TOTAL_BYTES = 24 * 1024 * 1024
TOP_K = 6
MIN_SCORE = 0.34
RESULT_CHARS = 7000

CODE_EXT = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".css", ".scss", ".sass", ".less", ".html", ".htm", ".json", ".md", ".txt",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".ps1", ".bat",
    ".sql", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".go", ".rs",
    ".php", ".rb", ".lua", ".xml", ".csv", ".env",
}

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
    avg = (sum(l for _tf, l in docs) / n) if n else 0.0
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


def _collect_files(root):
    """返回 [(abs, rel, size)]，跳过二进制、超大文件与扩展名白名单之外的。"""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext and ext not in CODE_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size <= 0 or size > MAX_FILE_BYTES:
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append((full, rel, size))
    out.sort(key=lambda x: x[2])                 # 小文件优先，保证覆盖率
    return out[:MAX_FILES]


def _signature(files):
    h = hashlib.sha1()
    h.update(("%s|%s|%s" % (INDEX_VERSION, CHUNK_LINES, CHUNK_OVERLAP)).encode())
    for full, rel, size in files:
        try:
            mtime = os.stat(full).st_mtime_ns
        except OSError:
            mtime = 0
        h.update(("%s|%d|%d\n" % (rel, size, mtime)).encode("utf-8", "ignore"))
    return h.hexdigest()


def _chunk_file(full, rel):
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    if _looks_binary(full):
        return []
    out = []
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    for start in range(0, max(1, len(lines)), step):
        block = lines[start:start + CHUNK_LINES]
        text = "\n".join(block).strip()
        if not text:
            continue
        # 路径也参与 embedding：问「chat.js 里怎么折叠」时能影响排序
        emb_text = "# %s:%d\n%s" % (rel, start + 1, text)
        out.append({"path": rel, "start": start + 1,
                    "end": min(len(lines), start + CHUNK_LINES),
                    "text": text, "emb": emb_text})
        if len(lines) <= start + CHUNK_LINES:
            break
    return out


# ---------------------------------------------------------------- 索引构建 / 持久化
def _cache_path(root):
    key = hashlib.sha1(root.lower().encode("utf-8", "ignore")).hexdigest()[:16]
    return os.path.join(INDEX_DIR, key + ".npz")


def _load_disk(root, sig):
    path = _cache_path(root)
    if not os.path.isfile(path):
        return None
    try:
        data = np.load(path, allow_pickle=False)
        if str(data["sig"]) != sig:
            return None
        meta = json.loads(str(data["meta"]))
        vec = data["vec"]
        if len(meta) != len(vec):
            return None
        return {"sig": sig, "meta": meta, "vec": vec}
    except Exception:
        return None


def _save_disk(root, idx):
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        path = _cache_path(root)
        tmp = path + ".tmp"
        np.savez_compressed(tmp, sig=np.array(idx["sig"]),
                            meta=np.array(json.dumps(idx["meta"], ensure_ascii=False)),
                            vec=idx["vec"])
        os.replace(tmp, path)
    except Exception:
        pass                                     # 缓存失败不影响本次检索


def _build(root, files, sig):
    chunks = []
    for full, rel, _size in files:
        chunks.extend(_chunk_file(full, rel))
        if len(chunks) >= MAX_CHUNKS:
            chunks = chunks[:MAX_CHUNKS]
            break
    if not chunks:
        return {"sig": sig, "meta": [], "vec": np.zeros((0, 1), dtype=np.float32)}
    vec = _embed_texts([c.pop("emb") for c in chunks])
    return {"sig": sig, "meta": chunks, "vec": vec}


def get_index(root):
    """取（必要时构建）索引。返回 (idx, files_total, files_indexed)。"""
    files = _collect_files(root)
    sig = _signature(files)
    cached = _CACHE.get(root)
    if cached and cached["sig"] == sig:
        return cached, len(files), cached.get("files", len(files))
    with _LOCK:
        cached = _CACHE.get(root)
        if cached and cached["sig"] == sig:
            return cached, len(files), cached.get("files", len(files))
        disk = _load_disk(root, sig)
        if disk:
            disk["files"] = len(files)
            _CACHE[root] = disk
            return disk, len(files), len(files)
        idx = _build(root, files, sig)
        idx["files"] = len(files)
        _CACHE[root] = idx
        _save_disk(root, idx)
        return idx, len(files), len(files)


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
