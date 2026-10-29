# -*- coding: utf-8 -*-
"""代码符号图：定义 / 引用 / 调用 / 测试映射（本地解析，不消耗 token）。

补的是 `find_symbol` 的正则短板：
- Python 用内置 `ast`：能区分「定义」和「调用」，能拿到作用域（`Class.method`）、
  签名、文档首行，不做纯文本匹配；
- JS / TS 没有内置解析器，仍用正则，结果标注为低置信度；
- 反向索引「谁调用了谁」，用来回答「改这里会影响什么」；
- 维护 import 表，把 `from app.agent import run_tool` 映射回 `agent`，用于找测试。

设计取舍：
- **不引入 tree-sitter**：Windows 上要预编译 wheel，而本项目 Python 占大头，
  `ast` 的精度更高且零依赖；JS 的误差用「低置信度」标注，不假装精确。
- **按文件增量**：磁盘缓存每个文件的解析结果，签名（大小 + mtime）没变就复用，
  只重解改动过的文件。
- **失败隔离**：解析失败（语法错误 / 编码问题）只跳过该文件，绝不让检索报错。
- **只读**：不写源码目录，缓存落在 `data/codeindex/`。
"""
import ast
import hashlib
import json
import os
import re
import threading

from app.store import DATA_DIR

INDEX_DIR = os.path.join(DATA_DIR, "codeindex")
INDEX_VERSION = 3
MAX_FILES = 1500
MAX_FILE_BYTES = 512 * 1024
MAX_REFS_PER_FILE = 600
MAX_JS_TEXT = 256 * 1024

PY_EXT = {".py", ".pyw"}
JS_EXT = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue"}

BLOCKED_SEGMENTS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                    ".browser_profile", ".idea", ".vscode", "dist", "build"}

# JS 里这些词出现频率极高，当引用会淹没真实结果
_JS_NOISE = {
    "if", "for", "while", "switch", "catch", "return", "function", "class", "const",
    "let", "var", "new", "typeof", "instanceof", "await", "async", "yield", "throw",
    "case", "else", "do", "try", "finally", "delete", "void", "in", "of", "this",
    "true", "false", "null", "undefined", "super", "import", "export", "default",
    "from", "as", "extends", "static", "get", "set", "constructor",
}

_JS_DEFS = (
    re.compile(r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+"
               r"([A-Za-z_$][\w$]*)\s*\(", re.M),
    re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
               r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M),
    re.compile(r"^[ \t]*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.M),
)
_JS_REF = re.compile(r"\b([A-Za-z_$][\w$]*)\b")
_JS_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")

_LOCK = threading.Lock()
_CACHE = {}                     # root -> {"sig": str, "files": {...}, "index": {...}}


# ---------------------------------------------------------------- 文件收集
def _blocked(path):
    for seg in str(path).replace("\\", "/").split("/"):
        if seg in BLOCKED_SEGMENTS:
            return True
    return False


def _collect(root):
    """返回 [(abs, rel, size, mtime)]，只要 .py / .js 系。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in BLOCKED_SEGMENTS]
        if _blocked(dirpath):
            continue
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in PY_EXT and ext not in JS_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size <= 0 or st.st_size > MAX_FILE_BYTES:
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append((full, rel, st.st_size, int(st.st_mtime)))
    out.sort(key=lambda x: (x[2], x[1]))
    return out[:MAX_FILES]


def _signature(files):
    h = hashlib.sha1()
    h.update(("v%d" % INDEX_VERSION).encode())
    for _full, rel, size, mtime in files:
        h.update(("%s|%d|%d\n" % (rel, size, mtime)).encode("utf-8", "ignore"))
    return h.hexdigest()


# ---------------------------------------------------------------- 解析
def _py_sig(node):
    a = node.args
    parts = [x.arg for x in getattr(a, "posonlyargs", []) + a.args]
    if parts and parts[0] in ("self", "cls"):
        parts = parts[1:]
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    parts.extend(x.arg for x in a.kwonlyargs)
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return "(" + ", ".join(parts) + ")"


def _py_doc(node):
    try:
        doc = ast.get_docstring(node) or ""
    except Exception:                        # noqa: BLE001
        return ""
    return doc.strip().splitlines()[0][:90] if doc.strip() else ""


class _PyVisitor(ast.NodeVisitor):
    """收集定义 / 引用 / 调用，并用栈维护作用域（Class.method）。"""

    def __init__(self):
        self.stack = []
        self.defs = []
        self.refs = []
        self.calls = []

    def _qual(self):
        return ".".join(self.stack)

    def _visit_def(self, node, kind):
        qual = ".".join(self.stack + [node.name])
        self.defs.append({
            "name": node.name, "qual": qual, "kind": kind,
            "line": node.lineno, "end": getattr(node, "end_lineno", node.lineno) or node.lineno,
            "sig": "" if kind == "class" else _py_sig(node),
            "doc": _py_doc(node),
        })
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):        # noqa: N802
        self._visit_def(node, "def")

    def visit_AsyncFunctionDef(self, node):   # noqa: N802
        self._visit_def(node, "def")

    def visit_ClassDef(self, node):           # noqa: N802
        self._visit_def(node, "class")

    def visit_Name(self, node):               # noqa: N802
        if isinstance(node.ctx, ast.Load) and len(self.refs) < MAX_REFS_PER_FILE:
            self.refs.append({"name": node.id, "qual": self._qual(), "line": node.lineno})
        self.generic_visit(node)

    def visit_Call(self, node):               # noqa: N802
        f = node.func
        name = ""
        if isinstance(f, ast.Name):
            name = f.id
        elif isinstance(f, ast.Attribute):
            name = f.attr
        if name and len(self.calls) < MAX_REFS_PER_FILE:
            self.calls.append({"name": name, "qual": self._qual(), "line": node.lineno})
        self.generic_visit(node)


def _parse_py(text):
    tree = ast.parse(text)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append({"local": a.asname or a.name.split(".")[0],
                                "target": a.name})
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for a in node.names:
                if a.name == "*":
                    continue
                imports.append({"local": a.asname or a.name,
                                "target": (mod + "." + a.name).strip(".")})
    v = _PyVisitor()
    v.visit(tree)
    return {"lang": "py", "defs": v.defs, "refs": v.refs,
            "calls": v.calls, "imports": imports}


def _parse_js(text):
    defs, refs, calls = [], [], []
    seen, def_lines = set(), set()
    for rx in _JS_DEFS:
        for m in rx.finditer(text):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            line = text.count("\n", 0, m.start()) + 1
            def_lines.add(line)
            sig = ""
            # `function name(` 后面直接跟括号；箭头函数 / class 抽不到，留空
            mm = re.match(r"\s*\(([^)]*)\)", text[m.end(1):m.end(1) + 200])
            if mm:
                sig = "(" + mm.group(1).strip() + ")"
            defs.append({"name": name, "qual": name, "kind": "def",
                         "line": line, "end": line, "sig": sig, "doc": ""})
    for m in _JS_REF.finditer(text):
        name = m.group(1)
        if name in _JS_NOISE or len(name) < 3:
            continue
        line = text.count("\n", 0, m.start()) + 1
        if line in def_lines:               # 定义行本身不算「被引用」
            continue
        if len(refs) < MAX_REFS_PER_FILE:
            refs.append({"name": name, "qual": "", "line": line})
    for m in _JS_CALL.finditer(text):
        name = m.group(1)
        if name in _JS_NOISE:
            continue
        line = text.count("\n", 0, m.start()) + 1
        if line in def_lines:
            continue
        if len(calls) < MAX_REFS_PER_FILE:
            calls.append({"name": name, "qual": "", "line": line})
    return {"lang": "js", "defs": defs, "refs": refs, "calls": calls, "imports": []}


def _parse(full):
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    if "\x00" in text[:4096]:
        return None
    ext = os.path.splitext(full)[1].lower()
    try:
        if ext in PY_EXT:
            return _parse_py(text)
        if ext in JS_EXT and len(text) <= MAX_JS_TEXT:
            return _parse_js(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    return None


# ---------------------------------------------------------------- 缓存
def _cache_path(root):
    key = hashlib.sha1(root.lower().encode("utf-8", "ignore")).hexdigest()[:16]
    return os.path.join(INDEX_DIR, "sym_" + key + ".json")


def _load_disk(root):
    path = _cache_path(root)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if int(data.get("version") or 0) != INDEX_VERSION:
            return {}
        return data.get("files") or {}
    except (OSError, ValueError):
        return {}


def _save_disk(root, files):
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        path = _cache_path(root)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": INDEX_VERSION, "files": files}, f,
                      ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass                                 # 缓存失败不影响本次结果


def _derive(files):
    """把「按文件」的数据翻成「按符号」的反向索引。"""
    idx = {}
    for rel, fd in files.items():
        for d in fd.get("defs") or []:
            idx.setdefault(d["name"], {"defs": [], "refs": [], "calls": [],
                                       "imports": []})["defs"].append((rel, d))
        for r in fd.get("refs") or []:
            idx.setdefault(r["name"], {"defs": [], "refs": [], "calls": [],
                                       "imports": []})["refs"].append((rel, r))
        for c in fd.get("calls") or []:
            idx.setdefault(c["name"], {"defs": [], "refs": [], "calls": [],
                                       "imports": []})["calls"].append((rel, c))
        for im in fd.get("imports") or []:
            tgt = (im.get("target") or "").split(".")[-1]
            if tgt:
                idx.setdefault(tgt, {"defs": [], "refs": [], "calls": [],
                                     "imports": []})["imports"].append((rel, im))
    return idx


def _ensure(root):
    """取（必要时增量重建）索引。返回 {"files", "index", "total", "parsed"}。"""
    files_now = _collect(root)
    sig = _signature(files_now)
    hit = _CACHE.get(root)
    if hit and hit["sig"] == sig:
        return hit
    with _LOCK:
        hit = _CACHE.get(root)
        if hit and hit["sig"] == sig:
            return hit
        old = _load_disk(root)
        files, sigs, parsed = {}, {}, 0
        for full, rel, size, mtime in files_now:
            fsig = "%d:%d" % (size, mtime)
            prev = old.get(rel) or {}
            if prev.get("sig") == fsig and prev.get("data"):
                files[rel] = prev["data"]
                sigs[rel] = prev
                continue
            data = _parse(full)
            if not data:
                continue
            files[rel] = data
            sigs[rel] = {"sig": fsig, "data": data}
            parsed += 1
        _save_disk(root, sigs)
        out = {"sig": sig, "files": files, "index": _derive(files),
               "total": len(files_now), "parsed": parsed}
        _CACHE[root] = out
        return out


# ---------------------------------------------------------------- 查询
def _hits(root, name, sub="", st=None):
    """按符号名取定义 / 引用 / 调用（可选限定路径前缀）。

    st 传入已构建的索引，避免批量查询时反复 walk 目录。
    """
    st = st or _ensure(root)
    idx = st["index"].get(name) or {"defs": [], "refs": [], "calls": []}
    prefix = (sub or "").replace("\\", "/").strip("/")
    keys = ("defs", "refs", "calls")
    if prefix:
        def keep(rel):
            return rel == prefix or rel.startswith(prefix + "/")
        return {k: [(r, d) for r, d in (idx.get(k) or []) if keep(r)] for k in keys}
    return {k: list(idx.get(k) or []) for k in keys}


def lookup(root, name, sub=""):
    """符号查询结果（结构化）。"""
    st = _ensure(root)
    got = _hits(root, name, sub)
    got["total"] = st["total"]
    got["parsed"] = st["parsed"]
    got["known"] = bool(got["defs"] or got["refs"] or got["calls"])
    return got


def boost_map(root, names, def_w=0.10, ref_w=0.03):
    """符号命中 → 文件加权，喂给 codeindex 的融合排序。

    Python 的跨模块使用是 `agent.run_tool(...)`，落在 calls 而不是 refs，
    所以两类都要算，否则「只给定义文件加权」会漏掉真正的使用方。
    """
    st = _ensure(root)
    out = {}
    for name in names or []:
        got = _hits(root, name, st=st)
        for rel, _d in got["defs"]:
            out[rel] = max(out.get(rel, 0.0), def_w)
        for rel, _r in got["refs"] + got["calls"]:
            out[rel] = max(out.get(rel, 0.0), ref_w)
    return out


def tests_for(root, target, limit=8):
    """给一个符号名或文件路径，返回可能覆盖它的测试文件。"""
    st = _ensure(root)
    files = st["files"]
    out = []

    def add(rel):
        if rel in out:
            return
        base = os.path.basename(rel)
        if rel.startswith("tests/") or base.startswith("test_") or base.endswith("_test.py"):
            out.append(rel)

    if "/" in target or "\\" in target or target.endswith((".py", ".js", ".ts")):
        rel = target.replace("\\", "/").lstrip("./")
        stem = os.path.splitext(os.path.basename(rel))[0]
        for cand in files:
            base = os.path.basename(cand)
            if cand == rel:
                continue
            if base in ("test_%s.py" % stem, "%s_test.py" % stem):
                add(cand)
        # 没有同名测试时，按「引用了该文件里多少个符号」排序 —— 比按目录顺序更相关
        scored = {}
        for n in [d["name"] for d in (st["files"].get(rel, {}).get("defs") or [])][:40]:
            node = st["index"].get(n) or {}
            for r, _x in (node.get("refs") or []) + (node.get("calls") or []):
                b = os.path.basename(r)
                if r != rel and (r.startswith("tests/") or b.startswith("test_")):
                    scored[r] = scored.get(r, 0) + 1
        for cand in sorted(scored, key=lambda k: -scored[k]):
            add(cand)
    else:
        got = _hits(root, target, st=st)
        # 符号被谁用到：定义 / 引用 / 调用都算（Python 跨模块用 `mod.fn()` 只会进 calls）
        for rel, _d in got["defs"] + got["refs"] + got["calls"]:
            add(rel)
    return out[:limit]


def impact(root, target, sub=""):
    """改这个符号 / 文件会影响什么：定义、引用、调用者、相关测试。"""
    st = _ensure(root)
    is_path = ("/" in target or "\\" in target
               or target.endswith((".py", ".js", ".ts", ".tsx", ".vue")))
    lines = []
    if is_path:
        rel = target.replace("\\", "/").lstrip("./")
        fd = st["files"].get(rel)
        lines.append("影响面（文件）：%s" % rel)
        if not fd:
            return ("没有索引到 %s（可能是被排除的目录 / 二进制 / 超过 %dKB）。"
                    % (rel, MAX_FILE_BYTES // 1024))
        lines.append("定义 %d 个，引用 %d 处，调用 %d 处%s"
                     % (len(fd.get("defs") or []), len(fd.get("refs") or []),
                        len(fd.get("calls") or []),
                        "（引用 / 调用已达单文件上限 %d，实际更多）" % MAX_REFS_PER_FILE
                        if len(fd.get("refs") or []) >= MAX_REFS_PER_FILE else ""))
        names = [d["name"] for d in (fd.get("defs") or [])][:40]
        users = {}
        index = st["index"]
        for n in names:
            node = index.get(n) or {}
            for r, _x in node.get("defs") or []:
                if r != rel:
                    users[r] = users.get(r, 0) + 1
            for r, _x in (node.get("refs") or []) + (node.get("calls") or []):
                if r == rel:                 # 同文件内部的使用不算「依赖它的文件」
                    continue
                users[r] = users.get(r, 0) + 1
        if users:
            lines.append("\n【直接依赖它的文件】")
            for r, cnt in sorted(users.items(), key=lambda x: -x[1])[:12]:
                lines.append("  %s  (%d 处)" % (r, cnt))
    else:
        got = _hits(root, target, sub, st=st)
        lines.append("影响面（符号）：%s" % target)
        if not (got["defs"] or got["refs"] or got["calls"]):
            return ("符号图里没有「%s」。可能是拼写不同、动态定义，或文件不在索引范围"
                    "（%d 个文件已解析）——不要据此断定它不存在，可用 search_files 再确认。"
                    % (target, st["total"]))
        lines.append("定义 %d 处，引用 %d 处，调用 %d 处"
                     % (len(got["defs"]), len(got["refs"]), len(got["calls"])))
        if got["defs"]:
            lines.append("\n【定义】")
            for rel, d in got["defs"][:12]:
                lines.append("  %s:%d  %s %s%s" % (
                    rel, d["line"], d["kind"], d["name"], d.get("sig") or ""))
                if d.get("doc"):
                    lines.append("      # " + d["doc"])
        if got["calls"]:
            lines.append("\n【调用者】")
            for rel, c in got["calls"][:15]:
                who = c.get("qual") or "(模块级)"
                lines.append("  %s:%d  %s 调用 %s" % (rel, c["line"], who, target))
        if got["refs"]:
            lines.append("\n【其他引用】")
            per = {}
            for rel, r in got["refs"]:
                if per.get(rel, 0) >= 2:
                    continue
                per[rel] = per.get(rel, 0) + 1
                lines.append("  %s:%d  (%s)" % (rel, r["line"], r.get("qual") or "模块级"))

    tests = tests_for(root, target)
    if tests:
        lines.append("\n【相关测试】")
        for t in tests:
            lines.append("  " + t)
        lines.append("\n改完建议先跑：python -m pytest %s -q" % " ".join(tests[:4]))
    else:
        lines.append("\n【相关测试】没找到直接对应的测试文件。")
    return "\n".join(lines)
