# -*- coding: utf-8 -*-
"""伪工具调用检测：只用于提醒模型改用原生调用，不参与执行。"""

from app.llm import pseudo_call_tagged


def test_action_style_xml_is_detected():
    assert pseudo_call_tagged('<run action="exec"><argv>["node","a.js"]</argv></run>')
    assert pseudo_call_tagged('<read action="file"><path>a.py</path></read>')
    assert pseudo_call_tagged('<edit action="patch">…</edit>')
    assert pseudo_call_tagged('<plan action="update">…</plan>')


def test_dsml_variants_are_detected():
    assert pseudo_call_tagged("<｜｜DSML｜｜tool_calls>")
    assert pseudo_call_tagged("<｜DSML｜tool_calls>")
    assert pseudo_call_tagged("<DSML｜tool_calls>")
    assert pseudo_call_tagged("< | DSML | tool_calls>")
    assert pseudo_call_tagged("</｜｜DS")


def test_tool_family_tags_are_detected():
    assert pseudo_call_tagged("<tool>read_file</tool>")
    assert pseudo_call_tagged("<invoke name=\"read_file\">")
    assert pseudo_call_tagged("<function_calls>")


def test_plain_text_and_html_are_not_detected():
    assert not pseudo_call_tagged("我读完了文件，下一步改第 57 行。")
    assert not pseudo_call_tagged("<div class=\"x\">hi</div>")
    assert not pseudo_call_tagged("a < b > c")
    assert not pseudo_call_tagged("")


def test_fenced_code_is_ignored():
    # 模型在解释协议时贴出写法，不该被当成伪调用
    text = "示例写法：\n```xml\n<run action=\"exec\">…</run>\n```\n上面只是说明。"
    assert not pseudo_call_tagged(text)
    # 围栏之外出现同样的写法才算
    assert pseudo_call_tagged(text + "\n<run action=\"exec\">node a.js</run>")
