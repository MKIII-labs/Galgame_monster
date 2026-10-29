# -*- coding: utf-8 -*-
"""探测本机可用的 PDF 生成链路：无头浏览器 / 中文字体 / matplotlib 中文嵌入。"""
import os
import sys

print("== python ==", sys.version.split()[0])

cands = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
]
print("\n== browsers ==")
for c in cands:
    print(("FOUND " if os.path.exists(c) else "no    ") + c)

fonts = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\SourceHanSansSC-Regular.otf",
]
print("\n== fonts ==")
for f in fonts:
    print(("FOUND " if os.path.exists(f) else "no    ") + f)

print("\n== matplotlib 中文嵌入测试 ==")
try:
    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["pdf.fonttype"] = 42
    matplotlib.rcParams["axes.unicode_minus"] = False
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    fp_path = None
    for f in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"]:
        if os.path.exists(f):
            fp_path = f
            break
    print("use font:", fp_path)
    fp = FontProperties(fname=fp_path) if fp_path else FontProperties()
    fig = plt.figure(figsize=(8.27, 11.69))
    fig.text(0.08, 0.92, "深圳大学 数学与应用数学（卓越班）", fontproperties=fp, fontsize=14)
    fig.text(0.08, 0.88, "Python / numpy / pandas / sympy", fontproperties=fp, fontsize=11)
    fig.savefig("_probe.pdf")
    plt.close(fig)
    print("matplotlib pdf ok:", os.path.getsize("_probe.pdf"), "bytes")
except Exception as e:
    print("matplotlib FAILED:", type(e).__name__, e)

print("\n== 文本提取测试 ==")
try:
    from pypdf import PdfReader
    r = PdfReader("_probe.pdf")
    print("pages:", len(r.pages))
    txt = r.pages[0].extract_text() or ""
    print("extracted repr:", repr(txt[:200]))
except Exception as e:
    print("pypdf FAILED:", type(e).__name__, e)
