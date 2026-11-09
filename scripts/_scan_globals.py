"""扫描 web/hall-stock.js 引用了哪些外部全局，供写渲染自检页打桩用。

只看「被调用」的标识符：定义为 function/const/let/var 的、以及 JS 关键字/内置对象排除掉。
输出按使用次数降序，人工扫一眼就知道要桩哪几个。
"""
import io
import re
import sys

src = io.open("web/hall-stock.js", encoding="utf-8").read()

# 本文件自己定义的顶层符号
defined = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", src, re.M))
defined |= set(re.findall(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)", src, re.M))
# 函数内局部变量也一并收（粗筛，够用）
defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)", src))
# 形参
for params in re.findall(r"\(([^()]*)\)\s*=>", src):
    defined |= {p.strip() for p in params.split(",") if p.strip()}

BUILTIN = {
    "window", "document", "console", "Math", "Number", "String", "Boolean", "Array",
    "Object", "JSON", "Date", "Promise", "Set", "Map", "RegExp", "Error", "isNaN",
    "parseInt", "parseFloat", "isFinite", "setTimeout", "setInterval", "clearTimeout",
    "clearInterval", "requestAnimationFrame", "cancelAnimationFrame", "fetch", "encodeURIComponent",
    "decodeURIComponent", "alert", "localStorage", "navigator", "location", "Infinity",
    "NaN", "undefined", "null", "true", "false", "this", "arguments", "of", "in", "new",
    "typeof", "instanceof", "return", "if", "else", "for", "while", "switch", "case",
    "break", "continue", "function", "const", "let", "var", "async", "await", "try",
    "catch", "finally", "throw", "delete", "void", "yield", "class", "extends", "super",
    "do", "default", "export", "import", "from", "get", "set", "static", "Intl",
    "SVGElement", "HTMLElement", "Node", "Event", "CustomEvent", "URL", "Blob", "FileReader",
    "AbortController", "structuredClone", "queueMicrotask", "parseFloat", "ResizeObserver",
    "MutationObserver", "IntersectionObserver", "performance", "screen", "history",
}

calls = re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", src)
count = {}
for name in calls:
    if name in defined or name in BUILTIN:
        continue
    count[name] = count.get(name, 0) + 1

print("外部全局（被调用，按次数降序）：")
for name, n in sorted(count.items(), key=lambda kv: -kv[1]):
    print("  %-24s %d" % (name, n))
if not count:
    print("  （无）")
