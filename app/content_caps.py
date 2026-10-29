# -*- coding: utf-8 -*-
"""内容能力：把笔记库、联网搜索、文档解析接入对话与任务引擎。

- `note.search` / `note.read`：对话与任务都可直接调用（只读）；
- `note.create` / `note.append`：写操作，chat=False，任务里必须人工确认；
- `search.web`：任务 / 工作流联网检索（对话已有自己的搜索流水线，避免重复）；
- `doc.read`：在沙箱内解析 txt / md / docx / xlsx / pdf（扫描件按需走视觉 OCR）。

这些能力都复用既有模块，不新增数据存储。
"""
import base64
import os

from app import agent, capabilities, docparse, imaging, notes as noteslib, search
from app.sessions import find_session, load_sessions

MAX_DOC_BYTES = 30 * 1024 * 1024
MAX_DOC_CHARS = 40000


def _sandbox_of(ctx):
    sid = (ctx or {}).get("session_id") or ""
    if not sid:
        return ""
    s = find_session(load_sessions(), sid)
    return agent.clean_sandbox((s or {}).get("sandbox"))


def _norm_note_path(raw):
    rel = str(raw or "").strip().replace("\\", "/").lstrip("/")
    if rel and not rel.lower().endswith(".md"):
        rel += ".md"
    return rel


# ------------------------------------------------------------
# 笔记
# ------------------------------------------------------------
def _note_search(args, ctx):
    q = str(args.get("query") or "").strip()
    if not q:
        return "❌ 缺少 query。"
    limit = max(1, min(int(args.get("limit") or 8), 30))
    hits = noteslib.search(q, limit=limit)
    if not hits:
        return "没有找到相关笔记。"
    lines = []
    for h in hits:
        line = "- [%s] %s" % (h.get("path") or "", h.get("name") or "")
        if h.get("snippet"):
            line += "：" + str(h["snippet"])[:120]
        lines.append(line)
    return "\n".join(lines)


def _note_read(args, ctx):
    rel = _norm_note_path(args.get("path"))
    if not rel:
        return "❌ 缺少 path。"
    text = noteslib.read_note(rel)
    if text is None:
        return "❌ 笔记不存在：%s" % rel
    limit = max(500, min(int(args.get("limit") or 12000), MAX_DOC_CHARS))
    if len(text) > limit:
        return text[:limit] + "\n\n（已截断，全文 %d 字）" % len(text)
    return text or "（空笔记）"


def _note_create(args, ctx):
    rel = _norm_note_path(args.get("path") or args.get("title"))
    if not rel:
        return "❌ 缺少 path。"
    ok, info = noteslib.create_note(rel, str(args.get("content") or ""))
    if not ok:
        return "❌ 创建笔记失败：%s" % info
    return "已创建笔记：%s" % info


def _note_append(args, ctx):
    rel = _norm_note_path(args.get("path") or args.get("title"))
    if not rel:
        return "❌ 缺少 path。"
    text = str(args.get("content") or "").strip()
    if not text:
        return "❌ 缺少 content。"
    old = noteslib.read_note(rel)
    if old is None:
        ok, info = noteslib.create_note(rel, text)
        return ("已创建笔记：%s" % info) if ok else ("❌ 创建笔记失败：%s" % info)
    merged = old.rstrip() + "\n\n" + text + "\n"
    if not noteslib.write_note(rel, merged):
        return "❌ 追加失败：%s" % rel
    return "已追加到笔记：%s" % rel


# ------------------------------------------------------------
# 联网搜索
# ------------------------------------------------------------
def _search_web(args, ctx):
    q = str(args.get("query") or "").strip()
    if not q:
        return "❌ 缺少 query。"
    provider, url, key, default_count = search.get_search_conf()
    if not key:
        return "❌ 未配置联网搜索密钥（设置 → 联网搜索）。"
    try:
        count = max(1, min(int(args.get("count") or default_count), 10))
    except (TypeError, ValueError):
        count = default_count
    try:
        items = search.search_web(q, provider, url, key, count)
    except Exception as e:
        return "❌ 搜索失败：%s" % e
    if not items:
        return "没有找到结果。"
    lines = []
    for i, it in enumerate(items, 1):
        lines.append("%d. %s\n   %s\n   %s" % (
            i, it.get("title") or "(无标题)", it.get("url") or "",
            str(it.get("snippet") or "")[:300]))
    return "\n".join(lines)


# ------------------------------------------------------------
# 文档解析
# ------------------------------------------------------------
def _doc_read(args, ctx):
    sandbox = _sandbox_of(ctx)
    if not sandbox:
        return "❌ 该会话还没有设置工作区，文档解析不可用。"
    try:
        path = agent.resolve_path(sandbox, args.get("path"))
    except agent.SandboxError as e:
        return "❌ %s" % e
    if not os.path.isfile(path):
        return "❌ 文件不存在：%s" % args.get("path")
    size = os.path.getsize(path)
    if size > MAX_DOC_BYTES:
        return "❌ 文件太大（%.1f MB），上限 30 MB。" % (size / 1048576.0)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return "❌ 读取失败：%s" % e

    ocr = None
    if imaging.vision_available() and str(args.get("ocr", "1")).lower() not in (
            "0", "false", "off", "no"):
        def _page(jpeg):
            b64 = base64.b64encode(jpeg).decode("ascii")
            txt = imaging.recognize_image_auto(
                "image/jpeg", b64,
                "这是文档的一页。请把这一页的全部文字逐字识别出来，保持原有顺序与分段；"
                "表格用 Markdown 表格、公式用 LaTeX。不要总结、不要补充、不要遗漏。")
            txt = (txt or "").strip()
            if txt.startswith(("⚠️", "❌")):
                return ""
            return txt
        ocr = _page

    text, warn = docparse.extract(os.path.basename(path), raw, ocr=ocr)
    if not text:
        return "❌ 解析失败：%s" % (warn or "未知原因")
    limit = max(1000, min(int(args.get("limit") or 12000), MAX_DOC_CHARS))
    if len(text) > limit:
        text = text[:limit] + "\n\n（已截断，全文 %d 字）" % len(text)
    if warn:
        text += "\n\n（提示：%s）" % warn
    return text


# ------------------------------------------------------------
# 注册
# ------------------------------------------------------------
capabilities.capability(
    "note.search", "搜索笔记库（标题 / 正文 / 路径）",
    {"query": "str", "limit": "int?"})(_note_search)
capabilities.capability(
    "note.read", "读取一篇笔记的 Markdown 内容",
    {"path": "str", "limit": "int?"})(_note_read)
capabilities.capability(
    "note.create", "新建一篇笔记（任务专用）",
    {"path": "str", "content": "str?"}, permission="write", chat=False)(_note_create)
capabilities.capability(
    "note.append", "向已有笔记追加内容；不存在则新建（任务专用）",
    {"path": "str", "content": "str"}, permission="write", chat=False)(_note_append)
capabilities.capability(
    "search.web", "联网搜索，返回标题 / 链接 / 摘要（任务专用）",
    {"query": "str", "count": "int?"}, chat=False)(_search_web)
capabilities.capability(
    "doc.read", "解析工作区内的文档（txt / md / docx / xlsx / pdf，扫描件可 OCR）",
    {"path": "str", "limit": "int?", "ocr": "str?"}, chat=False)(_doc_read)
