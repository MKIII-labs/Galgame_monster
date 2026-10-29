# -*- coding: utf-8 -*-
"""按 pyflakes 报告清理「imported but unused」：
用 ast 精确定位 import 节点，删掉指定别名后重写该语句；整条都空则删行。

    python scripts/clean_unused_imports.py <目标文件...>
"""
import ast
import io
import os
import re
import subprocess
import sys

PY = sys.executable


def pyflakes_unused(paths):
    """返回 {path: {name: None}}（只取 'imported but unused'）"""
    out = subprocess.run([PY, "-m", "pyflakes"] + paths, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    result = {}
    pat = re.compile(r"^(?P<p>.+?):(?P<line>\d+):(?P<col>\d+): '(?P<name>.+?)' imported but unused")
    for line in (out.stdout or "").splitlines():
        m = pat.match(line)
        if m:
            result.setdefault(m.group("p"), set()).add(m.group("name").split(".")[-1])
    return result


def render(module, aliases, level=0):
    """重写一条 import 语句"""
    parts = []
    for orig, asname in aliases:
        parts.append(orig if not asname or asname == orig else "%s as %s" % (orig, asname))
    dots = "." * level
    head = "from %s%s import " % (dots, module) if module is not None else "import "
    one = head + ", ".join(parts)
    if len(one) <= 100:
        return one + "\n"
    return head + "(" + ("\n    ".join(parts)) + ")\n"


def clean(path, drop):
    with io.open(path, "r", encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    edits = []          # (start_idx, end_idx, new_text_or_None)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        module = node.module if isinstance(node, ast.ImportFrom) else None
        level = getattr(node, "level", 0)
        kept, removed = [], False
        for a in node.names:
            local = a.asname or a.name.split(".")[0]
            if local in drop:
                removed = True
            else:
                kept.append((a.name, a.asname))
        if not removed:
            continue
        if kept:
            edits.append((node.lineno - 1, node.end_lineno, render(module, kept, level)))
        else:
            edits.append((node.lineno - 1, node.end_lineno, None))
    for start, end, text in sorted(edits, key=lambda t: -t[0]):
        lines[start:end] = [] if text is None else [text]
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    return len(edits)


def main(paths):
    total = 0
    for _ in range(4):                       # 迭代几轮，直到没有新的 unused
        rep = pyflakes_unused(paths)
        if not rep:
            break
        for path, names in rep.items():
            n = clean(path, names)
            total += n
            print("  %s: 清理 %d 条（%s）" % (os.path.relpath(path), n, ", ".join(sorted(names))))
    print("\n共处理 %d 条 import 语句" % total)


if __name__ == "__main__":
    main(sys.argv[1:])
