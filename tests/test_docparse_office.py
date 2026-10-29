# -*- coding: utf-8 -*-
"""Office 附件解析新增格式：pptx 抽取、csv/tsv 结构化。"""
import io
import zipfile

from app import docparse


def _pptx(slides):
    """造一个最小 pptx（本质是 zip + XML），不依赖 python-pptx。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, texts in enumerate(slides, 1):
            body = "".join(
                "<a:p><a:r><a:t>%s</a:t></a:r></a:p>" % t for t in texts)
            z.writestr(
                "ppt/slides/slide%d.xml" % i,
                "<p:sld xmlns:a='a'><p:cSld><p:spTree>%s</p:spTree></p:cSld></p:sld>" % body)
    return buf.getvalue()


def test_pptx_extracts_each_slide():
    raw = _pptx([["第一页标题", "第一页正文"], ["第二页标题"]])
    text, warn = docparse.extract("deck.pptx", raw)
    assert warn == ""
    assert "### 幻灯片 1" in text and "第一页标题" in text
    assert "### 幻灯片 2" in text and "第二页标题" in text
    # 顺序必须按页码，而不是 zip 里的写入顺序
    assert text.index("幻灯片 1") < text.index("幻灯片 2")


def test_pptx_without_text_reports_clear_error():
    raw = _pptx([[]])            # 一页但没有任何 <a:t>
    text, warn = docparse.extract("empty.pptx", raw)
    assert text is None
    assert "没有可提取的文本" in warn


def test_csv_becomes_markdown_table():
    raw = "科目,分数\n数学,92\n英语,88\n".encode("utf-8")
    text, warn = docparse.extract("score.csv", raw)
    assert warn == ""
    assert "| 科目 | 分数 |" in text
    assert "| 数学 | 92 |" in text
    assert "| 英语 | 88 |" in text


def test_csv_escapes_pipe_and_limits_rows():
    rows = ["a,b"] + ["x|y,%d" % i for i in range(docparse.MAX_CSV_ROWS + 5)]
    raw = ("\n".join(rows) + "\n").encode("utf-8")
    text, warn = docparse.extract("big.csv", raw)
    assert "只显示了前" in warn
    assert "x\\|y" in text                      # 单元格里的竖线要转义，否则表格错位
    assert text.count("\n") <= docparse.MAX_CSV_ROWS + 4


def test_tsv_uses_tab_delimiter():
    raw = "科目\t分数\n数学\t92\n".encode("utf-8")
    text, warn = docparse.extract("score.tsv", raw)
    assert warn == ""
    assert "| 科目 | 分数 |" in text
