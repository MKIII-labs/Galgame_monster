# -*- coding: utf-8 -*-
"""doc_create：docx / xlsx / pdf 生成落盘。"""
import zipfile

import pytest

from app import agent


def test_docx_blocks_and_table(tmp_path):
    text, undo, is_write = agent.run_tool(str(tmp_path), "doc_create", {
        "path": "out/report.docx",
        "title": "周报",
        "blocks": [
            {"type": "heading2", "text": "进展"},
            {"type": "paragraph", "text": "第一行\n第二行"},
            {"type": "bullet", "text": "甲\n乙"},
            {"type": "table", "rows": [["名称", "数量"], ["苹果", 3]]},
        ],
    })
    assert "已生成" in text
    assert undo and is_write
    p = tmp_path / "out" / "report.docx"
    assert p.is_file()
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        assert "word/document.xml" in names and "word/styles.xml" in names
        doc = z.read("word/document.xml").decode("utf-8")
    assert "周报" in doc and "苹果" in doc
    assert "<w:tbl>" in doc and "Heading2" in doc
    # 换行必须是 <w:br/>，不能把两行粘成一行
    assert "<w:br/>" in doc


def test_xlsx_sheets_and_types(tmp_path):
    text, undo, _ = agent.run_tool(str(tmp_path), "doc_create", {
        "path": "数据.xlsx",
        "sheets": [{"name": "统计", "rows": [["项目", "值"], ["销量", 12.5], ["有效", True]]}],
    })
    assert "已生成" in text
    p = tmp_path / "数据.xlsx"
    with zipfile.ZipFile(p) as z:
        assert "xl/workbook.xml" in z.namelist()
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        wb = z.read("xl/workbook.xml").decode("utf-8")
    assert 'name="统计"' in wb
    assert "inlineStr" in sheet and "<v>12.5</v>" in sheet and 't="b"' in sheet


def test_pdf_generates(tmp_path):
    pytest.importorskip("reportlab")
    from app import reader_export as rx
    if not rx._ensure_fonts():
        pytest.skip("没有可用的中文字体")
    text, _, _ = agent.run_tool(str(tmp_path), "doc_create", {
        "path": "报告.pdf",
        "title": "测试报告",
        "blocks": [{"type": "paragraph", "text": "正文一段中文。"},
                   {"type": "table", "rows": [["列1", "列2"], ["a", "b"]]}],
    })
    assert "已生成" in text
    data = (tmp_path / "报告.pdf").read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 800


def test_pptx_slides_and_xml_valid(tmp_path):
    text, _, _ = agent.run_tool(str(tmp_path), "doc_create", {
        "path": "deck.pptx",
        "title": "封面",
        "blocks": [
            {"type": "heading2", "text": "第一页"},
            {"type": "bullet", "text": "要点一\n要点二"},
            {"type": "slide", "title": "第三页", "bullets": ["a", "b"]},
        ],
    })
    assert "已生成" in text
    p = tmp_path / "deck.pptx"
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
        need = {"[Content_Types].xml", "ppt/presentation.xml",
                "ppt/slideMasters/slideMaster1.xml", "ppt/slideLayouts/slideLayout1.xml",
                "ppt/theme/theme1.xml", "ppt/slides/slide1.xml",
                "ppt/slides/slide2.xml", "ppt/slides/slide3.xml"}
        assert need <= names
        pres = z.read("ppt/presentation.xml").decode("utf-8")
        slide2 = z.read("ppt/slides/slide2.xml").decode("utf-8")
        rels = z.read("ppt/_rels/presentation.xml.rels").decode("utf-8")
        # 关系目标真的存在，否则 PowerPoint 会报文件损坏
        assert "slideMasters/slideMaster1.xml" in rels and "slides/slide3.xml" in rels
        import xml.etree.ElementTree as ET
        for n in names:
            if n.endswith((".xml", ".rels")):
                ET.fromstring(z.read(n))     # 所有部件都必须是合法 XML
    assert pres.count("<p:sldId ") == 3
    assert "要点一" in slide2 and "要点二" in slide2


def test_rejects_bad_extension_and_overwrite(tmp_path):
    text, _undo, _ = agent.run_tool(str(tmp_path), "doc_create",
                                    {"path": "a.txt", "blocks": []})
    assert "不支持" in text
    (tmp_path / "b.docx").write_bytes(b"x")
    text, _, _ = agent.run_tool(str(tmp_path), "doc_create",
                                {"path": "b.docx", "blocks": [{"text": "hi"}]})
    assert "已存在" in text
    text, _, _ = agent.run_tool(str(tmp_path), "doc_create",
                                {"path": "b.docx", "overwrite": True,
                                 "blocks": [{"text": "hi"}]})
    assert "已覆盖" in text
