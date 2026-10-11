# -*- coding: utf-8 -*-
"""会话沙箱内的「文件 Agent」——让模型在主对话里自主调用文件工具

- 协议：模型每轮回复末尾可追加一个 ```tool 代码块（JSON），说明要调用哪个工具。
  后端摘掉这个块，执行工具，把结果追加进对话，再让模型继续，直到它给出最终回答。
- 本模块负责：
  1. build_agent_prompt(s)  生成注入系统提示的工具说明（含沙箱路径与目录树）
  2. ToolExtractor          从流式文本里把 ```tool 块摘出来（用户看不到）
  3. resolve_path()         沙箱路径解析 + 越界 / 敏感目录拦截
  4. run_tool()             执行工具；写操作额外返回可撤回的变更记录
  5. new_ask / wait_answer  向用户提问并在原地等待回答
  6. undo(token)            撤回一次写操作（恢复文件快照）
  7. pick_directory()       弹出系统文件夹选择框
- 安全：一律不提供命令行工具；所有路径限制在沙箱内；data/ 目录永久禁止访问
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from app.mem_tool import MemoryExtractor, _loads_loose
from app.store import DATA_DIR

FENCE = "```tool"
MAX_ROUNDS = 8              # 单轮对话里最多循环多少次「调工具 → 再问模型」
MAX_READ_BYTES = 256 * 1024  # 单文件读取上限
MAX_RESULT_CHARS = 8000     # 单个工具结果回灌给模型时的截断长度
MAX_LIST_ENTRIES = 200
MAX_SEARCH_HITS = 60
MAX_CALLS_PER_ROUND = 6     # 模型单轮最多一次给出多少个工具调用
MAX_UNDO = 20               # 保留多少条撤销快照
ASK_TIMEOUT = 600           # 等待用户回答提问的超时（秒）

# 这些目录永远不进入（噪音 / 版本库 / 虚拟环境）
BLOCKED_SEGMENTS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                    ".browser_profile", ".idea", ".vscode"}

TOOLS = ("list_dir", "read_file", "search_files", "write_file", "edit_file",
         "make_dir", "move_file", "delete_file", "ask_user")
WRITE_TOOLS = ("write_file", "edit_file", "make_dir", "move_file", "delete_file")
# 允许「读取沙箱外文件」的工具（仅在用户开启授权后生效；写入类工具永远只在沙箱内）
OUTSIDE_READ_TOOLS = ("list_dir", "read_file")

_UNDO = {}
_undo_lock = threading.Lock()

_ASKS = {}
_asks_lock = threading.Lock()


class SandboxError(Exception):
    """沙箱路径校验失败"""
    pass


# ============================================================
# 0. 流式摘除与调用解析
# ============================================================

def ToolExtractor():
    """复用记忆工具的流式摘除器，只把起止标记换成 ```tool"""
    return MemoryExtractor(FENCE)


def parse_calls(block_text):
    """把 ```tool 块内容解析成 [{"tool": 名, "args": {...}}, ...]"""
    raw = re.sub(r"^\s*(json|tool)\s*", "", (block_text or "").strip(),
                 count=1, flags=re.I)
    data = _loads_loose(raw)
    if isinstance(data, dict):
        data = data["calls"] if isinstance(data.get("calls"), list) else [data]
    if not isinstance(data, list):
        return []
    out = []
    for item in data[:MAX_CALLS_PER_ROUND]:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool") or item.get("name")
        if not tool:
            continue
        out.append({"tool": str(tool).strip(),
                    "args": item.get("args") if isinstance(item.get("args"), dict) else {}})
    return out


# ============================================================
# 1. 沙箱路径解析与安全校验
# ============================================================

def _real(path):
    return os.path.realpath(os.path.abspath(path))


def _norm_rel(rel):
    return (rel or "").strip().replace("\\", "/")


def clean_sandbox(raw):
    """把沙箱字符串收敛成一个真实存在的目录路径。

    历史遗留或某些软件注入 stdout，会让沙箱值变成「路径 + 一堆日志」的多行文本；
    这里逐行尝试取第一个真实存在的目录；都不存在时返回首行，供报错展示。
    """
    s = str(raw or "").strip().strip('"').strip("'")
    if not s:
        return ""
    lines = [ln.strip().strip('"').strip("'") for ln in s.splitlines()]
    for ln in lines:
        if ln and os.path.isdir(_real(ln)):
            return ln
    return next((ln for ln in lines if ln), s)


def _assert_not_blocked(target):
    """绝对路径层面的硬性拦截：禁止目录段 + 本项目 data/"""
    for seg in str(target).replace("\\", "/").split("/"):
        if seg in BLOCKED_SEGMENTS:
            raise SandboxError("该目录被禁止访问：" + seg)
    data = _real(DATA_DIR)
    if target == data or target.startswith(data + os.sep):
        raise SandboxError("data 目录被禁止访问（含密钥等运行数据）")


def resolve_path(sandbox, rel, allow_outside=False):
    """把沙箱内路径解析成绝对路径。

    只接受**沙箱内**的路径：相对路径（推荐）与沙箱内的绝对路径都行（模型常直接引用
    提示里给出的沙箱绝对路径）；越界、命中禁止目录、或落在本项目 data/ 下时抛 SandboxError。

    allow_outside=True（用户已明确授权「读取沙箱外文件」）时：**绝对路径**可越过沙箱边界，
    但仍禁止 BLOCKED_SEGMENTS 与本项目 data/；相对路径依旧只在沙箱内解析。
    """
    sandbox = clean_sandbox(sandbox)
    root = _real(sandbox or "")
    if not sandbox or not os.path.isdir(root):
        raise SandboxError("沙箱目录不存在或未设置：" + (sandbox or "（空）"))
    raw = str(rel or "").strip().strip('"').strip("'")
    if raw.lower().startswith("file://"):
        raw = raw[7:]
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        target = _real(raw)                      # 绝对路径
        if target != root and not target.startswith(root + os.sep):
            if not allow_outside:
                raise SandboxError("路径越出沙箱范围：" + raw)
            _assert_not_blocked(target)          # 沙箱外：仅放行「读」，硬性拦截不变
            return target
    else:
        norm = _norm_rel(raw)
        base = os.path.basename(root)
        if base and (norm == base or norm.startswith(base + "/")):
            norm = norm[len(base):].lstrip("/")   # 容忍模型带上沙箱目录名
        target = _real(os.path.join(root, norm))
        if target != root and not target.startswith(root + os.sep):
            raise SandboxError("路径越出沙箱范围：" + raw)
    parts = os.path.relpath(target, root).replace("\\", "/").split("/")
    for seg in parts:
        if seg in BLOCKED_SEGMENTS:
            raise SandboxError("该目录被禁止访问：" + seg)
    data = _real(DATA_DIR)
    if target == data or target.startswith(data + os.sep):
        raise SandboxError("data 目录被禁止访问（含密钥等运行数据）")
    return target


def _read_text_file(path, limit=MAX_READ_BYTES):
    """读取文本文件，容忍常见编码（UTF-8 / GBK 等）。

    返回 (text, encoding, size, truncated)；截断可能切在多字节字符中间，最多回退 3 字节再解码。
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        raw = f.read(limit) if limit else f.read()
    truncated = bool(limit) and size > len(raw)
    first = ("utf-8-sig",) if raw[:3] == b"\xef\xbb\xbf" else ("utf-8",)   # 有 BOM 才用 sig
    for back in range(0, 4 if truncated else 1):
        data = raw[:len(raw) - back] if back else raw
        for enc in first + ("gb18030",):
            try:
                return data.decode(enc), enc, size, truncated
            except UnicodeDecodeError:
                continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)", size, truncated


def _rel(root, path):
    try:
        return os.path.relpath(path, _real(root)).replace("\\", "/")
    except ValueError:
        return path


def _walk(root):
    """遍历沙箱，跳过禁止目录与本项目 data 目录"""
    data = _real(DATA_DIR)
    for dirpath, dirnames, filenames in os.walk(_real(root)):
        here = _real(dirpath)
        if here == data or here.startswith(data + os.sep):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in BLOCKED_SEGMENTS]
        yield dirpath, dirnames, filenames


def _fmt_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


# ============================================================
# 2. 工具实现（每个返回 (结果文本, 撤销操作列表或 None)）
# ============================================================

def _snap(path):
    """记录文件原状，供撤销用"""
    if os.path.isdir(path):
        return {"kind": "restore_dir", "path": path, "existed": True}
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                return {"kind": "restore", "path": path, "existed": True, "data": f.read()}
        except OSError as e:
            raise SandboxError("无法读取原文件，已放弃修改：" + str(e))
    return {"kind": "restore", "path": path, "existed": False, "data": b""}


def _t_list_dir(root, args, allow_outside=False):
    p = resolve_path(root, args.get("path") or "", allow_outside)
    if not os.path.isdir(p):
        return "不是目录：" + _rel(root, p), None
    rows = []
    for name in sorted(os.listdir(p)):
        if name in BLOCKED_SEGMENTS:
            continue
        full = os.path.join(p, name)
        try:
            resolve_path(root, full, allow_outside)
        except SandboxError:
            continue
        try:
            is_dir = os.path.isdir(full)
            size = 0 if is_dir else os.path.getsize(full)
        except OSError:
            continue
        rows.append("%s  %s%s" % (_fmt_size(size) if not is_dir else "<目录>",
                                  name, "/" if is_dir else ""))
        if len(rows) >= MAX_LIST_ENTRIES:
            rows.append("…（已达 %d 条上限）" % MAX_LIST_ENTRIES)
            break
    head = "目录：%s（共 %d 项）" % (_rel(root, p) or ".", len(rows))
    return head + "\n" + ("\n".join(rows) if rows else "（空）"), None


def _t_read_file(root, args, allow_outside=False):
    p = resolve_path(root, args.get("path"), allow_outside)
    if not os.path.isfile(p):
        return "文件不存在：" + _rel(root, p), None
    text, enc, size, truncated = _read_text_file(p)
    lines = text.splitlines()
    try:
        start = max(1, int(args.get("start") or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        limit = int(args.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    chunk = lines[start - 1:start - 1 + limit] if limit > 0 else lines[start - 1:]
    width = len(str(start + max(len(chunk), 1) - 1))
    body = "\n".join("%*d | %s" % (width, start + i, s) for i, s in enumerate(chunk))
    head = "文件：%s（共 %d 行，%s%s，编码 %s），下面是第 %d 行起%s：" % (
        _rel(root, p), len(lines), _fmt_size(size),
        "，已截断" if truncated else "", enc, start,
        "（%d 行）" % len(chunk) if limit else "")
    tip = "\n注意：行号只是为了方便你定位，edit_file 的 old 参数必须用不含行号的原文。"
    return head + "\n" + body + tip, None


def _glob_to_regex(pat):
    """把 glob 转成匹配相对路径的正则；支持 ** 跨目录（* 不跨 /）。"""
    pat = (pat or "").replace("\\", "/").strip()
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3          # **/ 匹配任意层（含 0 层）
        elif pat.startswith("**", i):
            out.append(".*"); i += 2
        elif pat[i] == "*":
            out.append("[^/]*"); i += 1
        elif pat[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pat[i])); i += 1
    return re.compile("^" + "".join(out) + "$")


def _looks_binary(path, probe=4096):
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(probe)
    except OSError:
        return True


def _t_search_files(root, args):
    pattern = (args.get("pattern") or "").strip()
    globpat = (args.get("glob") or "").strip()
    if not pattern and not globpat:
        return "请至少提供 pattern（内容关键字）或 glob（文件名匹配）", None
    try:
        rx = re.compile(pattern) if pattern else None
    except re.error:
        rx = re.compile(re.escape(pattern)) if pattern else None
    glob_rx = _glob_to_regex(globpat) if globpat else None
    hits = []
    for dirpath, _dirnames, filenames in _walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rp = _rel(root, full)
            # glob 同时匹配「相对路径」与「文件名」（*.py 也能命中深层文件；**/*.py 亦可）
            if glob_rx and not (glob_rx.match(rp) or glob_rx.match(fn)):
                continue
            if rx is None:
                hits.append("%s  （文件名匹配 %s）" % (rp, globpat))
            else:
                if rx.search(fn):
                    hits.append("%s  （文件名命中）" % rp)
                if not _looks_binary(full):          # 只搜文本，跳过图片等二进制
                    try:
                        text, _enc, _size, _tr = _read_text_file(full)
                    except OSError:
                        text = ""
                    for i, line in enumerate(text.splitlines(), 1):
                        if rx.search(line):
                            hits.append("%s:%d: %s" % (rp, i, line.strip()[:160]))
                            break
            if len(hits) >= MAX_SEARCH_HITS:
                break
        if len(hits) >= MAX_SEARCH_HITS:
            break
    if not hits:
        return "未找到匹配（沙箱：%s）" % root, None
    return "共 %d 条（最多列 %d 条）：\n%s" % (
        len(hits), MAX_SEARCH_HITS, "\n".join(hits[:MAX_SEARCH_HITS])), None


def _t_write_file(root, args):
    p = resolve_path(root, args.get("path"))
    if os.path.isdir(p):
        return "目标是目录，不能写入：" + _rel(root, p), None
    content = args.get("content")
    if content is None:
        return "缺少 content 参数", None
    content = str(content)
    undo_op = _snap(p)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    existed = undo_op.get("existed")
    tail = "\n".join(("%d | %s" % (i, s)) for i, s in
                     enumerate(content.splitlines()[:40], 1))
    return ("已%s %s（%d 行，%d 字节）\n%s" % (
        "覆盖" if existed else "创建", _rel(root, p),
        len(content.splitlines()), len(content.encode("utf-8")),
        tail + ("\n…（略）" if len(content.splitlines()) > 40 else ""))), [undo_op]


def _t_edit_file(root, args):
    p = resolve_path(root, args.get("path"))
    if not os.path.isfile(p):
        return "文件不存在：" + _rel(root, p), None
    old = args.get("old")
    new = args.get("new")
    if old is None or new is None:
        return "缺少 old / new 参数", None
    old, new = str(old), str(new)
    text, enc, _size, _tr = _read_text_file(p, limit=0)
    cnt = text.count(old)
    if cnt == 0:
        return ("未找到要替换的内容（注意 old 必须是原文，且不含行号前缀）：\n"
                + old[:200]), None
    if cnt > 1:
        return ("old 在多处出现（%d 处），请给出更长的唯一片段" % cnt), None
    undo_op = _snap(p)
    write_enc = enc if enc in ("utf-8", "utf-8-sig", "gb18030") else "utf-8"
    with open(p, "w", encoding=write_enc, newline="") as f:
        f.write(text.replace(old, new, 1))
    add = len(new.splitlines())
    dele = len(old.splitlines())
    return ("已修改 %s（−%d 行 / +%d 行）" % (_rel(root, p), dele, add)), [undo_op]


def _t_make_dir(root, args):
    p = resolve_path(root, args.get("path"))
    if os.path.isdir(p):
        return "目录已存在：" + _rel(root, p), None
    if os.path.isfile(p):
        return "同名文件已存在：" + _rel(root, p), None
    try:
        os.makedirs(p, exist_ok=False)
    except OSError as e:
        return "创建目录失败：" + str(e), None
    return "已创建目录：" + _rel(root, p), [{"kind": "rmdir", "path": p}]


def _t_move_file(root, args):
    src = resolve_path(root, args.get("from"))
    dst = resolve_path(root, args.get("to"))
    if not os.path.exists(src):
        return "源不存在：" + _rel(root, src), None
    if os.path.exists(dst):
        return "目标已存在，拒绝覆盖：" + _rel(root, dst), None
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as e:
        return "移动失败：" + str(e), None
    return "已移动：%s → %s" % (_rel(root, src), _rel(root, dst)), \
        [{"kind": "move_back", "src": src, "dst": dst}]


def _t_delete_file(root, args):
    p = resolve_path(root, args.get("path"))
    if p == _real(root):
        return "不能删除沙箱根目录", None
    if os.path.isdir(p):
        if os.listdir(p):
            return "目录非空，拒绝删除（请先逐个删除其中的文件）", None
        try:
            os.rmdir(p)
        except OSError as e:
            return "删除目录失败：" + str(e), None
        return "已删除空目录：" + _rel(root, p), [{"kind": "mkdir", "path": p}]
    if not os.path.isfile(p):
        return "文件不存在：" + _rel(root, p), None
    undo_op = _snap(p)
    try:
        os.remove(p)
    except OSError as e:
        return "删除失败：" + str(e), None
    return "已删除文件：" + _rel(root, p), [undo_op]


_HANDLERS = {
    "list_dir": _t_list_dir,
    "read_file": _t_read_file,
    "search_files": _t_search_files,
    "write_file": _t_write_file,
    "edit_file": _t_edit_file,
    "make_dir": _t_make_dir,
    "move_file": _t_move_file,
    "delete_file": _t_delete_file,
}


def run_tool(sandbox, name, args, allow_outside=False):
    """执行一个工具。返回 (结果文本, 撤销操作列表或 None, 是否写操作)

    allow_outside=True 时，读取类工具（list_dir / read_file）可用绝对路径读取沙箱外文件。
    """
    name = (name or "").strip()
    if name not in _HANDLERS:
        return "未知工具：%s（可用：%s）" % (name, "、".join(TOOLS)), None, False
    sandbox = clean_sandbox(sandbox)      # 统一清洗，保证各工具算相对路径时用的是干净根目录
    args = args if isinstance(args, dict) else {}
    try:
        if allow_outside and name in OUTSIDE_READ_TOOLS:
            text, undo_ops = _HANDLERS[name](sandbox, args, True)
        else:
            text, undo_ops = _HANDLERS[name](sandbox, args)
    except SandboxError as e:
        return "❌ 沙箱拒绝：" + str(e), None, False
    except Exception as e:
        return "❌ 工具执行出错：%s" % e, None, False
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n…（结果过长已截断）"
    return text, undo_ops, name in WRITE_TOOLS


# ============================================================
# 3. 撤销
# ============================================================

def register_undo(sid, undo_ops, label):
    """把一次写操作的撤销信息登记进内存，返回 token"""
    if not undo_ops:
        return None
    token = "a_" + uuid.uuid4().hex[:10]
    with _undo_lock:
        _UNDO[token] = {"sid": sid, "ops": undo_ops, "label": label, "ts": time.time()}
        while len(_UNDO) > MAX_UNDO:
            _UNDO.pop(next(iter(_UNDO)))
    return token


def undo(token):
    """撤回一次写操作：按相反顺序还原文件"""
    with _undo_lock:
        item = _UNDO.pop(token, None)
    if not item:
        return False
    for op in reversed(item.get("ops") or []):
        kind = op.get("kind")
        try:
            if kind == "restore":
                if op.get("existed"):
                    parent = os.path.dirname(op["path"])
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with open(op["path"], "wb") as f:
                        f.write(op.get("data") or b"")
                elif os.path.exists(op["path"]):
                    os.remove(op["path"])
            elif kind == "restore_dir":
                if not os.path.exists(op["path"]):
                    os.makedirs(op["path"], exist_ok=True)
            elif kind == "rmdir":
                if os.path.isdir(op["path"]) and not os.listdir(op["path"]):
                    os.rmdir(op["path"])
            elif kind == "mkdir":
                if not os.path.exists(op["path"]):
                    os.makedirs(op["path"], exist_ok=True)
            elif kind == "move_back":
                if os.path.exists(op["dst"]) and not os.path.exists(op["src"]):
                    parent = os.path.dirname(op["src"])
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    os.replace(op["dst"], op["src"])
        except OSError:
            pass
    return True


# ============================================================
# 4. 向用户提问（原地等待回答）
# ============================================================

def new_ask(sid, question, options):
    qid = "q_" + uuid.uuid4().hex[:10]
    opts = [str(o) for o in (options or []) if str(o).strip()][:6]
    with _asks_lock:
        _ASKS[qid] = {"event": threading.Event(), "answer": None, "sid": sid,
                      "question": question, "options": opts}
    return qid, opts


def submit_answer(qid, answer):
    with _asks_lock:
        item = _ASKS.get(qid)
    if not item:
        return False
    item["answer"] = answer
    item["event"].set()
    return True


def wait_answer(qid, timeout=ASK_TIMEOUT, stop_event=None):
    """阻塞等待用户回答；返回回答文本，超时/被停止返回 None"""
    with _asks_lock:
        item = _ASKS.get(qid)
    if not item:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        if item["event"].wait(0.3):
            break
        if stop_event is not None and stop_event.is_set():
            break
    with _asks_lock:
        _ASKS.pop(qid, None)
    return item.get("answer")


# ============================================================
# 5. 提示词
# ============================================================

def _tree(sandbox, max_lines=60):
    """生成浅层目录树（最多两层），让模型知道沙箱里大致有什么"""
    lines = []
    root = _real(sandbox)
    for dirpath, dirnames, filenames in _walk(root):
        depth = os.path.relpath(dirpath, root).replace("\\", "/").count("/")
        if os.path.relpath(dirpath, root) == ".":
            depth = -1
        if depth >= 2:
            dirnames[:] = []
            continue
        prefix = "  " * (depth + 1)
        rel = _rel(root, dirpath)
        if rel != ".":
            lines.append(prefix + rel + "/")
        for fn in sorted(filenames)[:30]:
            lines.append(prefix + ("  " if rel != "." else "") + fn)
            if len(lines) >= max_lines:
                lines.append("…（目录树已截断）")
                return "\n".join(lines)
    return "\n".join(lines) or "（沙箱为空）"


def _full_agent_prompt(sandbox, allow_outside=False):
    """完整的「文件 Agent」说明（仅在已开启 Agent 且设置了沙箱时使用）"""
    intro = ("你现在可以在上面这个沙箱目录内**自主读写文件**来完成用户的任务。"
             "路径优先用**沙箱内的相对路径**（如 src/main.py，推荐）；"
             "沙箱内的绝对路径也可以。")
    outside_lines = []
    if allow_outside:
        outside_lines = [
            "⚠ 用户已开启「允许读取沙箱外文件（只读）」：当用户**明确要求**读取沙箱外的某个"
            "文件或目录时，list_dir / read_file 可用**绝对路径**（如 D:/xxx/yyy.txt）读取沙箱外的"
            "内容；**不要主动去翻沙箱外的文件**。写入类工具（write_file / edit_file / make_dir / "
            "move_file / delete_file）**仍然只能在沙箱内**。",
        ]
    else:
        intro += "（越出沙箱的路径会被拒绝）"
    return "\n".join([
        "## 文件 Agent（沙箱：%s）" % sandbox,
        intro,
        *outside_lines,
        "",
        "需要调用工具时，在回复**末尾**追加一个 ```tool 代码块（用户看不到它，"
        "不要向用户解释这个格式）：",
        "```tool",
        '{"calls":[{"tool":"工具名","args":{...}}]}',
        "```",
        "后端会执行它并把结果回给你，然后你可以继续调用或给出最终回答。",
        "**一次可以放多个 calls，按顺序执行。** 不需要工具时就不要输出这个块，直接回答。",
        "",
        "可用工具：",
        "- list_dir     {path}                        列出目录内容",
        "- read_file    {path, start?, limit?}        读取文件（带行号，最多 256KB）",
        "- search_files {pattern, glob?}              搜索文件：pattern 用正则搜**文件内容**，"
        "glob 按**文件名/相对路径**匹配（支持 `**/*.py`、`src/*.md`）；至少给一个。只搜文本文件",
        "- write_file   {path, content}               覆盖或创建文件（整文件写入）",
        "- edit_file    {path, old, new}              精确替换（old 在文件中必须唯一）",
        "- make_dir     {path}                        创建目录",
        "- move_file    {from, to}                    重命名 / 移动",
        "- delete_file  {path}                        删除文件或空目录",
        "- ask_user     {question, options[]}         向用户提问并等待回答",
        "",
        "规则：",
        "1. **先看再改**：改文件前先用 read_file / search_files 看清现状，不要凭空猜内容。",
        "2. edit_file 的 old 必须是文件里的**原文片段且唯一**，不要带行号前缀。",
        "3. 遇到不确定、有多个方案、或需要用户决策时，**用 ask_user 直接问**，"
        "不要自己瞎猜或停下来让用户「再说一次」。",
        "4. 不提供命令行工具，也不要假装执行了命令。",
        "5. `.git`、`.venv`、`node_modules`、`__pycache__` 与本项目 `data/` 目录禁止访问。",
        "6. 文本文件编码会自动识别（UTF-8 / GBK 等），read_file 会告诉你检测到的编码，"
        "edit_file 会**保持原编码**写回，不必担心乱码。",
        "7. 完成后用简洁的中文总结你改了什么、为什么这么改。",
        "",
        "沙箱目录树（供你判断结构）：",
        _tree(sandbox),
    ])


# 提问工具说明：与 Agent 解耦，任何模式下都注入，让 AI 能弹「提问卡」
_ASK_PROMPT = "\n".join([
    "## 提问工具（可随时使用，与文件 Agent 无关）",
    "当你**不确定、存在多个方案、或需要用户做决定**时，不要用普通文字提问、也不要自己瞎猜，"
    "而是在回复**末尾**追加一个 ```tool 代码块（用户看不到它，不要解释这个格式），"
    "后端会弹出一张**提问卡**并**原地等待**用户回答：",
    "```tool",
    '{"calls":[{"tool":"ask_user","args":{"question":"你的问题","options":["选项1","选项2"]}}]}',
    "```",
    "用户回答后你会收到结果并继续。`options` 可选（最多 6 个，用户也可以自己输入）。",
    "没有要问的就不要输出这个块。",
])


def build_agent_prompt(s):
    """生成注入系统提示的「工具」说明（提问始终可用）。"""
    agent_on = bool(s.get("agent_enabled"))
    sandbox = clean_sandbox(s.get("sandbox"))
    if agent_on and sandbox:
        return _full_agent_prompt(sandbox, bool(s.get("allow_outside_read")))
    if agent_on:
        return _ASK_PROMPT + ("\n\n## 文件 Agent\n已开启 Agent 但**尚未指定沙箱目录**；"
                              "如需读写文件，请先请用户在界面上点「📁」选择目录。")
    return _ASK_PROMPT


# ============================================================
# 6. 系统文件夹选择框
# ============================================================

_PICK_MARK = "__GM_PICK__"
_PICKER_CODE = (
    "import sys, tkinter as tk\n"
    "from tkinter import filedialog\n"
    "r = tk.Tk(); r.withdraw()\n"
    "try: r.attributes('-topmost', True)\n"
    "except Exception: pass\n"
    "p = filedialog.askdirectory(initialdir=sys.argv[1] or None,"
    " title='选择 Agent 沙箱目录') or ''\n"
    "sys.stdout.write('\\n" + _PICK_MARK + "' + p + '\\n')\n"
    "sys.stdout.flush()\n"
)


def pick_directory(initial=""):
    """弹出系统文件夹选择框；取消或失败返回空串。

    注意：只认带 __GM_PICK__ 标记的那一行——本机某些软件（如联想 AI Agent）会往
    Python 子进程的 stdout 里打日志，直接取整个 stdout 会把这些日志混进路径。
    """
    start = initial if (initial and os.path.isdir(initial)) else os.path.expanduser("~")
    try:
        r = subprocess.run([sys.executable, "-c", _PICKER_CODE, start],
                           capture_output=True, text=True, timeout=600)
    except Exception as e:
        print("[Agent] 目录选择框打开失败：%s" % e)
        return ""
    for line in reversed((r.stdout or "").splitlines()):
        if line.startswith(_PICK_MARK):
            path = line[len(_PICK_MARK):].strip().strip('"').strip("'")
            return path if (path and os.path.isdir(path)) else ""
    return ""


# ============================================================
# 7. IDE / 工作区：目录树与文件读写（沙箱内，复用同一套安全闸）
# ============================================================

def _sandbox_root(sandbox):
    root = _real(clean_sandbox(sandbox))
    if not sandbox or not os.path.isdir(root):
        raise SandboxError("沙箱目录不存在或未设置：" + (clean_sandbox(sandbox) or "（空）"))
    return root


def tree(sandbox, max_entries=2000, max_depth=6):
    """沙箱目录树（嵌套结构），跳过禁止目录与本项目 data/"""
    root = _sandbox_root(sandbox)
    count = [0]

    def walk(dirpath, depth):
        try:
            names = sorted(os.listdir(dirpath))
        except OSError:
            return []
        dirs, files = [], []
        for name in names:
            if name in BLOCKED_SEGMENTS or count[0] >= max_entries:
                continue
            full = os.path.join(dirpath, name)
            try:
                resolve_path(sandbox, _rel(root, full))
            except SandboxError:
                continue
            count[0] += 1
            (dirs if os.path.isdir(full) else files).append(name)
        out = []
        for name in dirs:
            node = {"name": name, "dir": True, "children": []}
            if depth < max_depth:
                node["children"] = walk(os.path.join(dirpath, name), depth + 1)
            out.append(node)
        for name in files:
            try:
                size = os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                size = 0
            out.append({"name": name, "dir": False, "size": size})
        return out

    return {"root": root, "children": walk(root, 0), "truncated": count[0] >= max_entries}


# 可在 IDE 里预览的图片类型 → MIME
IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".avif": "image/avif",
}
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def image_mime(rel):
    """按扩展名返回图片 MIME；非图片返回 None"""
    return IMAGE_MIME.get(os.path.splitext(str(rel or ""))[1].lower())


def read_bytes(sandbox, rel, limit=MAX_IMAGE_BYTES):
    """读取图片等二进制文件的原始字节（用于预览），带沙箱校验与大小上限"""
    p = resolve_path(sandbox, rel)
    if os.path.isdir(p):
        raise SandboxError("这是目录，不是文件")
    if not os.path.isfile(p):
        raise SandboxError("文件不存在：" + str(rel))
    size = os.path.getsize(p)
    if size > limit:
        raise SandboxError("文件过大（%.1f MB），暂不支持预览" % (size / 1048576.0))
    with open(p, "rb") as f:
        return f.read(), size


def read_text(sandbox, rel):
    """读一个文本文件（用于编辑器），容忍 UTF-8 / GBK 等编码"""
    p = resolve_path(sandbox, rel)
    if os.path.isdir(p):
        raise SandboxError("这是目录，不是文件")
    if not os.path.isfile(p):
        raise SandboxError("文件不存在：" + str(rel))
    if _looks_binary(p):
        raise SandboxError("这是二进制文件，无法在编辑器里打开")
    text, enc, size, truncated = _read_text_file(p, limit=2 * 1024 * 1024)
    return {"text": text, "encoding": enc, "size": size, "truncated": truncated}


def _write_enc(p):
    """沿用文件原编码写回；新文件用 utf-8"""
    if os.path.isfile(p):
        try:
            _t, e, _s, _r = _read_text_file(p, limit=0)
            if e in ("utf-8", "utf-8-sig", "gb18030"):
                return e
        except OSError:
            pass
    return "utf-8"


def write_text(sandbox, rel, content):
    """保存文本到文件（保持原编码）"""
    p = resolve_path(sandbox, rel)
    if os.path.isdir(p):
        raise SandboxError("目标是目录，不能写入")
    enc = _write_enc(p)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(p, "w", encoding=enc, newline="") as f:
        f.write(content if content is not None else "")
    return {"encoding": enc, "path": _rel(_sandbox_root(sandbox), p)}


def ensure_entry(sandbox, rel, is_dir=False):
    """新建空文件或空目录（已存在则报错）"""
    p = resolve_path(sandbox, rel)
    if os.path.exists(p):
        raise SandboxError("已存在：" + str(rel))
    if is_dir:
        os.makedirs(p, exist_ok=False)
    else:
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8"):
            pass
    return True


def rename_entry(sandbox, rel, new_name):
    """在同一目录内重命名"""
    new_name = (new_name or "").strip()
    if not new_name or "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        raise SandboxError("新名称不合法")
    src = resolve_path(sandbox, rel)
    parent_rel = os.path.dirname(_norm_rel(rel))
    dst = resolve_path(sandbox, os.path.join(parent_rel, new_name) if parent_rel else new_name)
    if os.path.exists(dst):
        raise SandboxError("目标已存在：" + new_name)
    os.replace(src, dst)
    return True


def delete_entry(sandbox, rel):
    """删除文件或空目录（非空目录拒绝）"""
    p = resolve_path(sandbox, rel)
    if p == _sandbox_root(sandbox):
        raise SandboxError("不能删除沙箱根目录")
    if os.path.isdir(p):
        if os.listdir(p):
            raise SandboxError("目录非空，请先删除里面的内容")
        os.rmdir(p)
        return True
    if not os.path.isfile(p):
        raise SandboxError("不存在：" + str(rel))
    os.remove(p)
    return True
