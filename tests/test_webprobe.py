# -*- coding: utf-8 -*-
"""render_check（无头浏览器渲染自检）：注入、解析、报告与工具接线"""
import json

import pytest

from app import agent, webprobe


# ---------------------------------------------------------------- 视口
def test_viewport_default():
    assert webprobe.parse_viewport(None) == webprobe.DEFAULT_VIEWPORT
    assert webprobe.parse_viewport("") == webprobe.DEFAULT_VIEWPORT
    assert webprobe.parse_viewport("随便写") == webprobe.DEFAULT_VIEWPORT


def test_viewport_parses_and_clamps():
    assert webprobe.parse_viewport("1280x800") == (1280, 800)
    assert webprobe.parse_viewport("375,667") == (375, 667)
    # 过窄 / 过高都夹回安全范围
    assert webprobe.parse_viewport("10x10") == (webprobe.MIN_W, webprobe.MIN_H)
    assert webprobe.parse_viewport("9999x9999") == (webprobe.MAX_W, webprobe.MAX_H)


# ---------------------------------------------------------------- 注入
def test_inject_adds_base_and_probes():
    html = "<html><head><title>t</title></head><body><p>x</p></body></html>"
    out = webprobe.inject_probe(html, r"D:\work\site")
    assert out.count(webprobe.PROBE_ID) >= 2          # 探针脚本里两处引用
    assert "<base href=" in out
    assert "site/" in out                              # base 指回原目录
    assert out.index("<base") < out.index("<title>")    # 插在 head 开头
    assert out.rindex("</body>") > out.index("__galProbe")   # 尾脚本在 body 结束前


def test_inject_without_head_or_body():
    out = webprobe.inject_probe("<p>裸片段</p>", r"D:\x")
    assert "<head>" in out and "</head>" in out
    assert out.rstrip().endswith("</script>") or "</script>" in out


def test_dir_uri_handles_cjk_and_space():
    uri = webprobe._dir_uri(r"D:\我的 项目\网页")
    assert uri.startswith("file:///D:/")
    assert " " not in uri and "%20" in uri


# ---------------------------------------------------------------- 解析
def test_extract_probe_takes_last_match():
    dom = ('<pre id="__probe">{"v":1}</pre>'
           '<pre id="__probe">{&quot;v&quot;:2}</pre>')
    assert webprobe.extract_probe(dom) == {"v": 2}


def test_extract_probe_missing_or_broken():
    assert webprobe.extract_probe("") is None
    assert webprobe.extract_probe("<pre id=\"__probe\">not json</pre>") is None


# ---------------------------------------------------------------- 报告
def test_report_lists_errors_and_issues():
    data = {
        "logs": ["error: WEAPON_INFO is not defined @ index.html:2104"],
        "counts": {"elements": 1204, "scrollHeight": 2410},
        "issues": [
            {"k": "零尺寸容器", "v": ["canvas#game 0×0"]},
            {"k": "被遮挡", "v": ["button#start ← 被 div#cover 挡住"]},
            {"k": "字号过小", "v": []},
        ],
    }
    text = webprobe.format_report(data, "index.html", (1280, 800))
    assert "index.html" in text and "1204" in text and "2410" in text
    assert "WEAPON_INFO" in text
    assert "零尺寸容器（1）" in text and "canvas#game 0×0" in text
    assert "字号过小" not in text            # 空分类不出现
    assert "仍需人看" in text or "配色" in text


def test_report_clean_page():
    data = {"logs": [], "counts": {"elements": 10, "scrollHeight": 800}, "issues": []}
    text = webprobe.format_report(data, "a.html", (1280, 800))
    assert "运行时错误：无" in text
    assert "未发现" in text


# ---------------------------------------------------------------- 工具接线
def test_tool_registered():
    assert "render_check" in agent.TOOLS
    assert "render_check" in agent._HANDLERS
    assert "render_check" not in agent.WRITE_TOOLS
    assert "render_check" not in agent.RUN_TOOLS      # 不走命令审批


def test_aliases_fold_to_render_check():
    for name in ("render", "screenshot", "render_page", "rendercheck"):
        assert agent.normalize_tool_name(name) == "render_check"
    assert agent.normalize_tool_name("render_check") == "render_check"


def test_native_schema_gated_by_agent():
    on = [t["function"]["name"] for t in agent.native_tool_schemas(agent_on=True)]
    off = [t["function"]["name"] for t in agent.native_tool_schemas(agent_on=False)]
    assert "render_check" in on
    assert "render_check" not in off


def test_describe_call_is_readonly():
    d = agent.describe_call("render_check", {"path": "index.html", "viewport": "375x667"})
    assert d["risk"] == "只读"
    assert "index.html" in d["text"] and "375x667" in d["text"]


def test_render_check_rejects_outside_sandbox(tmp_path):
    sbox = tmp_path / "box"
    sbox.mkdir()
    (sbox / "a.html").write_text("<html><body>x</body></html>", encoding="utf-8")
    text, _undo, _wrote = agent.run_tool(str(sbox), "render_check", {"path": "../evil.html"})
    # 工具协议 v2：失败统一带稳定错误码
    assert "SANDBOX_DENIED" in text
    assert "越出沙箱范围" in text


# ---------------------------------------------------------------- 拦截非法输入
def test_check_without_browser(monkeypatch):
    monkeypatch.setattr(webprobe, "edge_path", lambda: "")
    text = webprobe.check(__file__)
    assert "没找到 Edge" in text and "GAL_EDGE" in text


def test_check_rejects_non_html(monkeypatch):
    monkeypatch.setattr(webprobe, "edge_path", lambda: "dummy-browser")
    text = webprobe.check(__file__)
    assert "只支持 .html" in text


# ---------------------------------------------------------------- 真机（无浏览器则跳过）
@pytest.mark.skipif(not webprobe.edge_path(), reason="本机没有 Edge / Chrome")
def test_real_render_detects_zero_canvas_and_error(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(
        "<!doctype html><html><head><meta charset='utf-8'></head><body>"
        "<canvas id='game' width='0' height='0'></canvas>"
        "<button id='b' style='position:absolute;left:10px;top:10px'>开始</button>"
        "<div style='position:absolute;left:0;top:0;width:200px;height:60px;background:#eee'></div>"
        "<script>setTimeout(function(){document.getElementById('nope').boom();},100);</script>"
        "</body></html>", encoding="utf-8")
    text = webprobe.check(str(page))
    assert "零尺寸容器" in text
    assert "canvas#game 0×0" in text
    assert "error" in text                      # 运行时错误被探针捕获
    assert "被遮挡" in text
