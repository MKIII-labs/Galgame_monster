# -*- coding: utf-8 -*-
"""Archify 集成：schema 取用、候选校验、finalize 全链路与工具接线。"""
import os
import shutil

import pytest

from app import agent, archify, webprobe

EXAMPLES = os.path.join(archify.SKILL_DIR, "examples")
HAS_RUNTIME = archify.available()[0]


def test_version_and_schema():
    assert archify.version() == "3.2.0"
    text = archify.schema_text("architecture")
    assert "architecture.schema.json" in text and "common.schema.json" in text
    assert "示例" in text
    assert "components" in text


def test_schema_without_example_and_bad_type():
    text = archify.schema_text("workflow", with_example=False)
    assert "workflow.schema.json" in text and "### 示例" not in text
    assert archify.schema_text("nope").startswith("❌")


def test_schema_text_tells_model_to_use_chinese():
    text = archify.schema_text("workflow", with_example=False)
    assert "默认用中文" in text and "不要照抄" in text
    assert "zh-CN" in text


def test_apply_locale_json_adds_and_preserves(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"meta": {"title": "x"}}', encoding="utf-8")
    assert archify._apply_locale(str(p)) is True
    obj = __import__("json").loads(p.read_text(encoding="utf-8"))
    assert obj["meta"]["locale"] == "zh-CN" and obj["meta"]["title"] == "x"
    assert archify._apply_locale(str(p)) is False       # 幂等

    p2 = tmp_path / "b.json"
    p2.write_text('{"meta": {"locale": "en"}}', encoding="utf-8")
    assert archify._apply_locale(str(p2)) is False
    assert __import__("json").loads(
        p2.read_text(encoding="utf-8"))["meta"]["locale"] == "en"

    p3 = tmp_path / "c.json"
    p3.write_text('{"diagram_type": "workflow"}', encoding="utf-8")
    assert archify._apply_locale(str(p3)) is True
    assert __import__("json").loads(
        p3.read_text(encoding="utf-8"))["meta"]["locale"] == "zh-CN"


def test_apply_locale_markdown_inserts_after_title(tmp_path):
    p = tmp_path / "a.archify.md"
    p.write_text("# 标题\n\n正文\n", encoding="utf-8")
    assert archify._apply_locale(str(p)) is True
    assert p.read_text(encoding="utf-8").startswith("# 标题\n@meta locale=zh-CN")

    p2 = tmp_path / "b.archify.md"
    p2.write_text("# T\n@meta style=compact\n", encoding="utf-8")
    assert archify._apply_locale(str(p2)) is True
    assert "@meta style=compact locale=zh-CN" in p2.read_text(encoding="utf-8")

    p3 = tmp_path / "c.archify.md"
    p3.write_text("# T\n@meta locale=en\n", encoding="utf-8")
    assert archify._apply_locale(str(p3)) is False


def test_inline_candidate_written_in_sandbox(tmp_path):
    path = archify.prepare_inline("architecture", '{"meta": {}}', str(tmp_path))
    assert path.startswith(str(tmp_path))
    assert path.endswith("candidate.json")
    with open(path, "r", encoding="utf-8") as f:
        assert f.read() == '{"meta": {}}'
    md = archify.prepare_inline("explainer", "# Hi", str(tmp_path))
    assert md.endswith("candidate.archify.md")


def test_writes_files_gate():
    assert agent.writes_files("archify", {"action": "schema"}) is False
    assert agent.writes_files("archify", {"action": "doctor"}) is False
    assert agent.writes_files("archify", {"action": "finalize"}) is True
    assert agent.writes_files("archify", {}) is True
    assert agent.writes_files("write_file") is True


@pytest.mark.skipif(not HAS_RUNTIME, reason="no vendored archify/node")
def test_finalize_invalid_candidate_fails_validate(tmp_path):
    cand = tmp_path / "bad.json"
    cand.write_text('{"meta": {}}', encoding="utf-8")
    text, rep = archify.finalize("architecture", str(cand),
                                 str(tmp_path / "bad.html"), str(tmp_path))
    assert rep and rep.get("ok") is False
    assert "未通过" in text and "validate" in text
    assert not (tmp_path / "bad.html").exists()


@pytest.mark.skipif(not (HAS_RUNTIME and webprobe.edge_path()),
                    reason="no vendored archify/node/browser")
def test_finalize_example_end_to_end(tmp_path):
    src = os.path.join(EXAMPLES, "web-app.architecture.json")
    cand = tmp_path / "web-app.json"
    shutil.copyfile(src, str(cand))
    out = tmp_path / "web-app.html"
    text, rep = archify.finalize("architecture", str(cand), str(out), str(tmp_path))
    assert rep.get("ok") is True, text[-2000:]
    assert out.is_file() and out.stat().st_size > 100000
    gates = rep.get("gates") or {}
    assert all(gates.get(k) == "pass" for k in archify.GATE_ORDER)
    assert "__ARCHIFY_METRICS__" in text and "门槛：" in text
    # 候选里没写 locale：finalize 自动补 zh-CN，Viewer 界面走中文目录
    html = out.read_text(encoding="utf-8", errors="replace")
    assert 'lang="zh-CN"' in html
    assert "由 Archify 生成的架构图" in html


@pytest.mark.skipif(not (HAS_RUNTIME and webprobe.edge_path()),
                    reason="no vendored archify/node/browser")
def test_tool_wiring_finalize_with_candidate_path(tmp_path):
    src = os.path.join(EXAMPLES, "cache-miss-request.sequence.json")
    cand = tmp_path / "cache.json"
    shutil.copyfile(src, str(cand))
    text, undo, is_write = agent.run_tool(
        str(tmp_path), "archify",
        {"action": "finalize", "type": "sequence",
         "candidate_path": "cache.json", "output": "diagram/cache.html"},
        caps={"sid": "pytest_archify"})
    assert is_write is True and undo is None
    assert "门槛" in text and "browser-check ✓" in text
    assert (tmp_path / "diagram" / "cache.html").is_file()


def test_tool_schema_action(tmp_path):
    text, undo, is_write = agent.run_tool(
        str(tmp_path), "archify",
        {"action": "schema", "type": "erd", "example": False},
        caps={"sid": "pytest_archify"})
    assert "erd.schema.json" in text
    assert undo is None and is_write is False


def test_archify_does_not_need_sandbox():
    """非 Agent 模式也能调用：不依赖沙箱，但仍受工具分层控制。"""
    assert agent.needs_sandbox("archify") is False
    assert agent.needs_sandbox("tool_search") is False

    def names(groups, agent_on=False):
        return [t["function"]["name"] for t in
                agent.native_tool_schemas(agent_on=agent_on, allow_groups=groups)]

    assert "archify" in names({"diagram"})
    assert "archify" not in names(set())
    assert "archify" in names({"diagram"}, agent_on=True)


def test_archify_without_sandbox_returns_artifact_marker(tmp_path, monkeypatch):
    """没有沙箱时落 data/archify/<sid>/，结果带产物标记供对话内嵌。"""
    monkeypatch.setattr(archify, "ROOT", str(tmp_path))

    def fake_finalize(kind, cand, output, root, quality="", repo_root="", timeout=0):
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, "wb") as f:
            f.write(b"<html><body>ok</body></html>")
        return "【Archify】workflow · 通过", {"ok": True, "artifact": {"bytes": 27}}

    monkeypatch.setattr(archify, "finalize", fake_finalize)
    text, undo, is_write = agent.run_tool(
        "", "archify",
        {"action": "finalize", "type": "workflow", "candidate": "{}"},
        caps={"sid": "s_nosandbox"})
    assert undo is None
    marker = [ln for ln in text.splitlines() if ln.startswith("__ARCHIFY_ARTIFACT__ ")]
    assert marker, text
    item = __import__("json").loads(marker[0][len("__ARCHIFY_ARTIFACT__ "):])
    assert item["url"].startswith("/archify/s_nosandbox/")
    assert item["type"] == "workflow" and item["bytes"] == 27
    assert (tmp_path / "data" / "archify" / "s_nosandbox" /
            item["path"].replace("/", os.sep)).is_file()


def test_read_for_view_blocks_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(archify, "ROOT", str(tmp_path))
    root = archify.workspace_root("s_view")
    sub = os.path.join(root, "d")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "a.html"), "wb") as f:
        f.write(b"<html><body>x</body></html>")
    raw = archify.read_for_view("s_view", "d/a.html")
    assert raw and b"__gmDiagram" in raw
    assert archify.read_for_view("s_view", "../../etc/passwd") is None
