# -*- coding: utf-8 -*-
"""把 resume.pdf 的第一页渲染成 PNG，用于目视检查真实排版效果。

pypdfium2 自带渲染，不依赖 poppler。
"""
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "resume.pdf"
OUT = ROOT / "resume_preview.png"


def main() -> int:
    if not PDF.exists():
        print(f"FAIL: 缺少 {PDF}")
        return 1

    doc = pdfium.PdfDocument(str(PDF))
    print(f"pages: {len(doc)}")

    page = doc[0]
    bitmap = page.render(scale=2)
    img = bitmap.to_pil()
    img.save(OUT)
    print(f"OK: {OUT} size={img.size}")

    # 顺带报一下页面尺寸（pt），确认是 A4
    box = page.get_size()
    print(f"page_size_pt: {box[0]:.1f} x {box[1]:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
