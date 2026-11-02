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
