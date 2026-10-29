# -*- coding: utf-8 -*-
"""校验 resume.pdf：页数、可提取中文文本、豆腐块检查。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "resume.pdf"


def main() -> int:
    if not PDF.exists():
        print("FAIL: resume.pdf 不存在")
        return 1
    size = PDF.stat().st_size
    if size == 0:
        print("FAIL: resume.pdf 是空文件")
        return 1
    print(f"OK: resume.pdf 存在，{size} bytes")

    from pypdf import PdfReader

    reader = PdfReader(str(PDF))
    pages = len(reader.pages)
    print(f"pages: {pages}")

    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    print(f"chars: {len(text)}")

    must = [
        "深圳大学",
        "数学与应用数学",
        "教育背景",
        "项目经历",
        "Green",
        "行列式",
        "sympy",
        "pandas",
        "技能",
    ]
    missing = [m for m in must if m not in text]
    if missing:
        print("FAIL: 缺失关键文本 ->", missing)
        return 1
    print("OK: 关键中文文本全部可提取")

    # 豆腐块 / 乱码代理项检查
    tofu = [ch for ch in text if ch in "\ufffd\u25a1"]
    if tofu:
        print(f"FAIL: 存在豆腐块字符 {len(tofu)} 处")
        return 1
    print("OK: 无豆腐块乱码")

    if pages != 1:
        print(f"FAIL: 页数为 {pages}，期望 1 页")
        return 1
    print("OK: 单页 A4")

    print("\n--- 提取文本预览（前 400 字）---")
    print(text[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
