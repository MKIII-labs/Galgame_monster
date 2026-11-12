# -*- coding: utf-8 -*-
"""PDF 页级读取：extract_pages 存页、attachment_read(pages=) 按页返回。"""
import io

import pytest

from app import attachments, docparse
from app.agent.tools_files import _parse_page_ranges, _t_attachment_read


def _make_pdf(pages=3):
    pytest.importorskip("reportlab", reason="需要 reportlab（测试依赖）")
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, "Page %d unique text %d" % (i + 1, i + 1))
        c.showPage()
    c.save()
    return buf.getvalue()


def test_extract_pages_returns_per_page_text():
    text, warn, pages = docparse.extract_pages("a.pdf", _make_pdf(3))
    assert text and "Page 1" in text and "Page 3" in text
    assert len(pages) == 3
    assert "Page 2" in pages[1]
    assert "Page 1" not in pages[1]
    assert warn == ""


def test_extract_pages_non_pdf_has_no_pages():
    text, warn, pages = docparse.extract_pages("a.txt", b"hello")
    assert text == "hello"
    assert pages == []


def test_parse_page_ranges():
    assert _parse_page_ranges("1-3,7", 10) == [1, 2, 3, 7]
    assert _parse_page_ranges("5-2", 10) == [2, 3, 4, 5]
    assert _parse_page_ranges("9", 3) == []
    assert len(_parse_page_ranges("1-100", 100)) == 20      # 每次最多 20 页


def test_attachment_read_pages(monkeypatch):
    item = {"id": "att_x", "kind": attachments.DOC, "name": "a.pdf",
            "text": "P1\n\nP2\n\nP3", "pages": ["P1", "P2", "P3"]}
    monkeypatch.setattr(attachments, "find", lambda sid, key: item)

    out, _ = _t_attachment_read(None, {"id": "att_x", "pages": "2"},
                                caps={"sid": "s1"})
    assert "第 2 页" in out and "P2" in out
    assert "P1" not in out and "P3" not in out

    out, _ = _t_attachment_read(None, {"id": "att_x", "pages": "1-2"},
                                caps={"sid": "s1"})
    assert "第 1 页" in out and "第 2 页" in out
    assert "共 3 页" in out

    out, _ = _t_attachment_read(None, {"id": "att_x", "pages": "9"},
                                caps={"sid": "s1"})
    assert "页码范围无效" in out


def test_attachment_read_pages_without_page_data(monkeypatch):
    item = {"id": "att_x", "kind": attachments.DOC, "name": "a.txt",
            "text": "hello", "pages": []}
    monkeypatch.setattr(attachments, "find", lambda sid, key: item)
    out, _ = _t_attachment_read(None, {"id": "att_x", "pages": "1"},
                                caps={"sid": "s1"})
    assert "没有页级数据" in out
