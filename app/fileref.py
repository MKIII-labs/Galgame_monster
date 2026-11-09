# -*- coding: utf-8 -*-
"""输入框引用：`#` 菜单的 chip 描述 + 旧式 `@路径` 文本，都在这里展开成本轮上下文。

为什么放在服务端
----------------
前端只有路径 / 定位信息，不具备 docparse 的抽取能力；扫描件、Office 文件、
超大文档、越界路径这些情况也需要一处统一兜底，否则「引用了但模型读不到」
会表现成模型胡说。

约定
----
- `from_client(refs, …)`  chip 引用（type = file / folder / code / terminal /
  git / problems / rule / doc / memory / workspace），正文在发送时现取
- `@out/main.pdf`        旧式整份文档引用（超过上限自动截断）
- `@out/main.pdf#3`      旧式只引用第 3 页（PDF）
- 解析不到、或不是文件路径（比如 `@某人`）时保持原文不动，不打扰用户
- 每个文件、每轮请求都有字符上限，防止一次引用把上下文撑爆
"""
import io
import os
import re

from app import agent, docparse

MAX_REF_CHARS = 12000        # 单个文件注入上限（约 6k token）
MAX_REF_TOTAL = 30000        # 本轮所有引用合计上限
MAX_REF_FILES = 8
MAX_REF_LINES = 400          # #code 行区间上限
MAX_TERMINAL_CHARS = 8000

# 末尾的标点是中文输入常见的，不该算进路径
_MENTION_RE = re.compile(r"@([^\s@，。；、！？：（）()\[\]【】「」]+)")
_PAGE_RE = re.compile(r"^(.*?)#(?:p)?(\d{1,4})$")
RICH_EXT = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"}
TEXT_EXT = {".txt", ".md", ".markdown", ".tex", ".csv", ".tsv", ".json", ".jsonl",
            ".yaml", ".yml", ".log", ".ini", ".conf", ".html", ".htm", ".xml",
            ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".java", ".c",
            ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh",
            ".bat", ".ps1", ".sql", ".lua", ".kt", ".swift"}


def parse_mentions(text):
    """从文本里取出 (路径, 页码) 列表；页码为 0 表示整份。"""
    out = []
    for m in _MENTION_RE.finditer(str(text or "")):
        raw = (m.group(1) or "").strip().strip('"\'')
        raw = raw.rstrip(".,;:!?")
        if not raw:
            continue
        page = 0
        pm = _PAGE_RE.match(raw)
        if pm:
            raw, page = pm.group(1), int(pm.group(2))
        out.append((raw, page))
    return out


def _pdf_page_text(raw, page):
    """取 PDF 指定页的文本；失败返回空串（调用方回退到整份文本）。"""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if 1 <= page <= len(reader.pages):
            return (reader.pages[page - 1].extract_text() or "").strip()
    except Exception:                    # noqa: BLE001  页码读不到就退整份
        pass
    return ""


def _read_one(path, page, sandbox):
    """读取并抽取一个引用文件。返回 dict；不像文件路径时返回 None。"""
    try:
        p = agent.resolve_path(sandbox, path, for_read=True)
    except Exception as e:               # noqa: BLE001
        if "/" in path or "\\" in path or os.path.splitext(path)[1]:
            return {"path": path, "page": page, "text": "", "warn": str(e)}
        return None
    if not os.path.isfile(p):
        if "/" in path or "\\" in path or os.path.splitext(path)[1]:
            return {"path": path, "page": page, "text": "", "warn": "文件不存在"}
        return None

    ext = os.path.splitext(p)[1].lower()
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError as e:
        return {"path": path, "page": page, "text": "", "warn": "读取失败：%s" % e}

    text, warn = docparse.extract(os.path.basename(p), raw)
    if page and ext == ".pdf":
        ptext = _pdf_page_text(raw, page)
        if ptext:
            text, warn = ptext, ""
    if not text:
        return {"path": path, "page": page, "text": "",
                "warn": warn or "没有可提取的文本（可能是扫描件）"}
    text = text.strip()
    truncated = len(text) > MAX_REF_CHARS
    if truncated:
        text = text[:MAX_REF_CHARS] + "\n…（已截断）"
    return {"path": path, "page": page, "text": text, "warn": warn or "",
            "chars": len(text), "truncated": truncated, "ext": ext}


def expand(text, sandbox):
    """按 `@路径` 抽取正文；返回可直接挂到消息上的 refs 列表。"""
    refs = []
    if not text or not sandbox:
        return refs
    seen = set()
    total = 0
    for path, page in parse_mentions(text):
        if len(refs) >= MAX_REF_FILES:
            break
        key = (path, page)
        if key in seen:
            continue
        seen.add(key)
        if total >= MAX_REF_TOTAL:
            break
        one = _read_one(path, page, sandbox)
        if not one:
            continue
        body = one.get("text") or ""
        if body and total + len(body) > MAX_REF_TOTAL:
            keep = max(0, MAX_REF_TOTAL - total)
            one["text"] = body[:keep] + "\n…（本轮引用总量已达上限，后续内容未注入）"
            one["truncated"] = True
        total += len(one.get("text") or "")
        refs.append(one)
    return refs


# ============================================================
# 输入框 chip 引用（# 菜单）：前端只传定位信息，正文在服务端现取。
# 这样引用的是「发送时」的文件内容，而不是点菜单那一刻的旧快照。
# ============================================================

def folder_refs(sandbox, rel, limit=12, max_chars=20000):
    """把一个目录下的文本文件作为引用（最多 limit 个 / max_chars 字）。"""
    if not sandbox:
        return []
    try:
        root = agent.resolve_path(sandbox, rel, for_read=True)
    except Exception:                        # noqa: BLE001
        return []
    if not os.path.isdir(root):
        return []
    skip = {".git", ".venv", "node_modules", "__pycache__", "data", "dist", "build"}
    out, total = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    txt = f.read(6000)
            except OSError:
                continue
            if not txt.strip():
                continue
            relpath = os.path.relpath(full, root).replace("\\", "/")
            body = txt.strip()
            total += len(body)
            prefix = str(rel or "").replace("\\", "/").strip("/")
            out.append({"path": (prefix + "/" + relpath) if prefix else relpath,
                        "text": body})
            if len(out) >= limit or total >= max_chars:
                return out
    return out


def code_ref(sandbox, path, start=0, end=0, fallback_text=""):
    """`#code` 行区间：读文件后取 [start, end]（1 起，含两端）。"""
    try:
        s = max(1, int(start or 0)) if start else 0
        e = max(0, int(end or 0))
    except (TypeError, ValueError):
        s, e = 0, 0
    text = ""
    if path and sandbox:
        try:
            p = agent.resolve_path(sandbox, path, for_read=True)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
        except Exception:                    # noqa: BLE001
            text = ""
    from_file = bool(text)
    if not text:
        text = str(fallback_text or "")
        if not text.strip():
            return None
    if s and from_file:
        lines = text.replace("\r\n", "\n").split("\n")
        e = e or s
        e = min(max(e, s), s + MAX_REF_LINES - 1, len(lines))
        if s > len(lines):
            return None
        text = "\n".join(lines[s - 1:e])
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_REF_CHARS:
        text = text[:MAX_REF_CHARS] + "\n…（已截断）"
    ref = {"path": path or "选中代码", "text": text}
    if s:
        ref["start"] = s
        ref["end"] = e
    return ref


def problems_ref(items):
    rows = []
    for it in (items or [])[:40]:
        if not isinstance(it, dict):
            continue
        rows.append("%s:%s [%s] %s" % (
            it.get("path") or "?", it.get("line") or 0,
            it.get("severity") or "warning", it.get("message") or ""))
    text = "\n".join(rows) if rows else "（当前没有诊断问题）"
    return {"path": "IDE 问题", "virtual": True, "text": text[:12000]}


def doc_ref(kb_id, node_id):
    """知识库条目引用：按 id 精确取一条，取不到再按标题模糊匹配。"""
    try:
        from app import knowledge
    except Exception:                        # noqa: BLE001
        return None
    kb_id = str(kb_id or "").strip()
    node_id = str(node_id or "").strip()
    kb = knowledge.load_kb(kb_id) if kb_id else None
    if not kb:
        return None
    nodes = kb.get("nodes") or []
    node = next((n for n in nodes if str(n.get("id") or "") == node_id), None)
    if node is None and node_id:
        node = next((n for n in nodes
                     if str(n.get("title") or "").strip() == node_id), None)
    if not node:
        return None
    title = str(node.get("title") or node_id or "知识库条目")
    body = str(node.get("content") or "").strip()
    if not body:
        return None
    if len(body) > MAX_REF_CHARS:
        body = body[:MAX_REF_CHARS] + "\n…（已截断）"
    return {"path": "知识库：" + title, "virtual": True, "text": body}


def memory_ref(nodes, key):
    """会话记忆节点引用：按 id / 标题取一条。"""
    key = str(key or "").strip()
    if not key:
        return None
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if str(n.get("id") or "") == key or str(n.get("title") or "").strip() == key:
            title = str(n.get("title") or key)
            body = str(n.get("content") or "").strip()
            if not body:
                return None
            if len(body) > MAX_REF_CHARS:
                body = body[:MAX_REF_CHARS] + "\n…（已截断）"
            return {"path": "记忆：" + title, "virtual": True, "text": body}
    return None


def _virtual(title, text, limit=MAX_REF_CHARS):
    body = str(text or "").strip()
    if not body:
        return None
    if len(body) > limit:
        body = body[:limit] + "\n…（已截断）"
    return {"path": title, "virtual": True, "text": body}


def from_client(refs, sandbox, problems=None, memory_nodes=None):
    """把前端 `#` 菜单产生的 chip 描述展开成可注入的 refs。

    返回 (refs, force_workspace)。不认识的类型直接跳过，不阻断发送。
    """
    out = []
    force_ws = False
    total = 0
    for r in refs or []:
        if not isinstance(r, dict):
            continue
        if len(out) >= MAX_REF_FILES + 4:
            break
        if total >= MAX_REF_TOTAL:
            break
        kind = str(r.get("type") or "").strip().lower()
        one = None
        if kind == "workspace":
            force_ws = True
            continue
        if kind == "file":
            path = str(r.get("path") or "").strip()
            if path:
                one = _read_one(path, int(r.get("page") or 0), sandbox)
        elif kind == "folder":
            path = str(r.get("path") or "").strip()
            if path:
                out.extend(folder_refs(sandbox, path))
                total = sum(len(x.get("text") or "") for x in out)
                continue
        elif kind == "code":
            one = code_ref(sandbox, str(r.get("path") or "").strip(),
                           r.get("start"), r.get("end"),
                           str(r.get("text") or ""))
        elif kind == "terminal":
            one = _virtual("终端输出", r.get("text"), MAX_TERMINAL_CHARS)
        elif kind == "git":
            try:
                from app import git
                d = git.diff_text(sandbox, "work")
                one = _virtual("未提交改动（Git）", d.get("text"))
            except Exception as e:            # noqa: BLE001
                one = {"path": "未提交改动（Git）", "virtual": True, "text": "",
                       "warn": str(e)[:120]}
        elif kind == "problems":
            one = problems_ref(problems)
        elif kind == "rule":
            rules = ""
            try:
                rules = agent.project_rules(sandbox) if sandbox else ""
            except Exception:                 # noqa: BLE001
                rules = ""
            one = _virtual("项目规则（AGENTS.md / CLAUDE.md）", rules, 20000)
        elif kind == "doc":
            one = doc_ref(r.get("kb"), r.get("node") or r.get("title"))
        elif kind == "memory":
            one = memory_ref(memory_nodes, r.get("id") or r.get("title"))
        if not one:
            continue
        body = one.get("text") or ""
        if body and total + len(body) > MAX_REF_TOTAL:
            keep = max(0, MAX_REF_TOTAL - total)
            one["text"] = body[:keep] + "\n…（本轮引用总量已达上限，后续内容未注入）"
            one["truncated"] = True
        total += len(one.get("text") or "")
        out.append(one)
    return out, force_ws

