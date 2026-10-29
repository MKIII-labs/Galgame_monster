# -*- coding: utf-8 -*-
"""仓库结构图（repo map）：按文件提取顶层函数 / 类与行号。

不依赖 tree-sitter，也不消耗 token：
- Python 用内置 `ast`，签名与行号精确；
- JS / TS 用正则，覆盖 `function` / 箭头函数赋值 / `class`；
- 其它文件只列文件名与行数。

用途：让模型先用一两千 token 看懂项目骨架，少读好几个文件。
"""
import ast
import os
import re

MAX_FILES = 60
MAX_SYMBOLS_PER_FILE = 25
MAX_CHARS = 6000

PY_EXT = {".py", ".pyw"}
JS_EXT = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue"}

_JS_PATTERNS = (
    re.compile(r"^[ \t]*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M),
    re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
               r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M),
    re.compile(r"^[ \t]*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", re.M),
)


def _py_args(node):
    a = node.args
    names = [x.arg for x in getattr(a, "posonlyargs", []) + a.args]
    names = [n for n in names if n not in ("self", "cls")]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        names.append("*")
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return "(" + ", ".join(names) + ")"


def _py_symbols(text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.lineno, "def", node.name + _py_args(node)))
        elif isinstance(node, ast.ClassDef):
            out.append((node.lineno, "class", node.name))
            for sub in node.body[:20]:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((sub.lineno, "  def", sub.name + _py_args(sub)))
    return out


def _js_symbols(text):
    out, seen = [], set()
    for idx, rx in enumerate(_JS_PATTERNS):
        for m in rx.finditer(text):
            name = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            if (line, name) in seen:
                continue
            seen.add((line, name))
            out.append((line, "function" if idx < 2 else "class", name))
    return out


def _symbols(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in PY_EXT and ext not in JS_EXT:
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(256 * 1024)
    except OSError:
        return []
    out = _py_symbols(text) if ext in PY_EXT else _js_symbols(text)
    return sorted(out, key=lambda x: x[0])


def _collect(root, sub=""):
    """收集代码文件（相对路径）。sub 为空时遍历全仓。"""
    files = []
    base = os.path.join(root, sub) if sub else root
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in
                       {".git", ".venv", "venv", "node_modules", "__pycache__",
                        ".browser_profile", ".idea", ".vscode", "vendor"}]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in PY_EXT and ext not in JS_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size <= 0 or size > 256 * 1024:
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            files.append((rel, full, size))
    # 入口 / 根目录文件优先，然后是浅层文件
    files.sort(key=lambda x: (x[0].count("/"), x[0]))
    return files


def _dir_overview(files, limit=10):
    counts = {}
    for rel, _full, _size in files:
        top = rel.split("/")[0] if "/" in rel else "(根目录)"
        counts[top] = counts.get(top, 0) + 1
    items = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return " · ".join("%s/ (%d)" % (k, v) for k, v in items if k != "(根目录)")


def build(sandbox, path="", max_files=MAX_FILES):
    """生成仓库结构文本。path 可限定到某个目录 / 文件。"""
    from app import agent                       # 延迟导入，避免循环依赖
    try:
        root = agent._real(agent._sandbox_root(sandbox))
    except Exception as e:
        return "沙箱不可用：%s" % e

    sub = ""
    single_file = False
    files = []
    if path:
        try:
            target = agent.resolve_path(root, path)
        except Exception as e:
            return "路径不可用：%s" % e
        if os.path.isdir(target):
            sub = os.path.relpath(target, root).replace("\\", "/")
            if sub == ".":
                sub = ""
            files = _collect(root, sub)
        elif os.path.isfile(target):
            single_file = True
            rel = os.path.relpath(target, root).replace("\\", "/")
            try:
                files = [(rel, target, os.path.getsize(target))]
            except OSError:
                files = []
        else:
            return "路径不存在：" + str(path)
    else:
        files = _collect(root)

    if not files:
        return "没有可分析的 Python / JS 文件" + ("（目录：%s）" % sub if sub else "")

    lines = []
    if not single_file:
        head = "仓库结构（%d 个代码文件）" % len(files)
        ov = _dir_overview(files)
        if ov:
            head += "\n目录：" + ov
        lines.append(head)

    used = 0
    shown = 0
    for rel, full, _size in files:
        if shown >= max(1, min(int(max_files or MAX_FILES), 200)):
            lines.append("…（还有 %d 个文件未列出，可用 path 指定目录）" % (len(files) - shown))
            break
        syms = _symbols(full)
        nlines = 0
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                nlines = sum(1 for _ in f)
        except OSError:
            nlines = 0
        block = "%s  (%d 行)" % (rel, nlines)
        if syms:
            body = ["  %5d  %s %s" % (ln, kind, name)
                    for ln, kind, name in syms[:MAX_SYMBOLS_PER_FILE]]
            if len(syms) > MAX_SYMBOLS_PER_FILE:
                body.append("        …（共 %d 个符号）" % len(syms))
            block += "\n" + "\n".join(body)
        if used + len(block) > MAX_CHARS:
            lines.append("…（输出达到 %d 字符上限，可用 path 缩小范围）" % MAX_CHARS)
            break
        used += len(block)
        shown += 1
        lines.append(block)
    return "\n\n".join(lines)
