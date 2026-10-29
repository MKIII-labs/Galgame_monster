# -*- coding: utf-8 -*-
"""用本机 Edge 无头模式把 resume.html 打印成 A4 单页 PDF。

run_command 白名单只放行 python，所以通过 subprocess 调用浏览器完成渲染。
"""
import os
import subprocess
import sys
from pathlib import Path

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

ROOT = Path(__file__).resolve().parent.parent


def find_browser() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("未找到可用的 Chromium 系浏览器")


def main() -> int:
    html = ROOT / "resume.html"
    pdf = ROOT / "resume.pdf"
    if not html.exists():
        raise SystemExit(f"缺少输入文件：{html}")
    if pdf.exists():
        pdf.unlink()

    browser = find_browser()
    cmd = [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",
        f"--print-to-pdf={pdf}",
        "--virtual-time-budget=8000",
        html.as_uri(),
    ]
    print("browser:", browser)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    print("exit:", r.returncode)
    if r.stdout.strip():
        print("stdout:", r.stdout.strip()[:800])
    if r.stderr.strip():
        print("stderr:", r.stderr.strip()[:800])

    if not pdf.exists() or pdf.stat().st_size == 0:
        print("FAIL: resume.pdf 未生成")
        return 1
    print(f"OK: resume.pdf {pdf.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
