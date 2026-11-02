# -*- coding: utf-8 -*-
"""通用验收契约：结构校验、安全执行、record、开工闸门与收尾提醒。"""
from app.api import chat


def _criterion(text="a.txt 包含 hello", tool="read_file", args=None, expect=None):
    return {"text": text,
            "check": {"tool": tool, "args": args or {"path": "a.txt"}},
            "expect": expect if expect is not None else {"contains": "hello"}}


def test_set_rejects_read_check_without_expect(tmp_path):
    s = {}
    text, changed = chat._acceptance_handle(
        s, {"action": "set", "criteria": [_criterion(expect={})]},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    assert not changed
    assert "expect" in text and "contains" in text


def test_set_validates_and_status(tmp_path):
    s = {}
    text, changed = chat._acceptance_handle(
        s, {"action": "set", "criteria": [_criterion()]},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    assert changed
    assert "已登记 1 条" in text
    crit = chat._acceptance_criteria(s)
    assert crit[0]["status"] == "pending"
    assert "pending" in chat._acceptance_status_text(s)


def test_run_safe_check_passes(tmp_path):
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    s = {}
    chat._acceptance_handle(
        s, {"action": "set", "criteria": [_criterion()]},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    text, changed = chat._acceptance_handle(
        s, {"action": "run"}, str(tmp_path), {"sid": "s1"}, agent_on=True)
    assert changed
    assert chat._acceptance_criteria(s)[0]["status"] == "passed"
    assert "通过 1" in text


def test_run_command_is_manual_then_record(tmp_path):
    s = {}
    crit = {"id": "cmd", "text": "测试命令通过",
            "check": {"tool": "run_command", "args": {"argv": ["node", "test.mjs"]}},
            "expect": {"exit_code": 0}}
    chat._acceptance_handle(
        s, {"action": "set", "criteria": [crit]},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    chat._acceptance_handle(
        s, {"action": "run"}, str(tmp_path), {"sid": "s1"}, agent_on=True)
    assert chat._acceptance_criteria(s)[0]["status"] == "manual"

    text, changed = chat._acceptance_handle(
        s, {"action": "record", "id": "cmd", "passed": True,
            "evidence": "exit 0, 12 tests passed"},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    assert changed
    assert chat._acceptance_criteria(s)[0]["status"] == "passed"
    assert "通过 1" in text


def test_record_requires_evidence(tmp_path):
    s = {}
    chat._acceptance_handle(
        s, {"action": "set", "criteria": [_criterion()]},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    text, changed = chat._acceptance_handle(
        s, {"action": "record", "id": "a1", "passed": True},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    assert not changed and "evidence" in text


def test_revise_requires_reason_and_updates(tmp_path):
    s = {}
    chat._acceptance_handle(
        s, {"action": "set", "criteria": [_criterion()]},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    text, changed = chat._acceptance_handle(
        s, {"action": "revise", "id": "a1", "reason": ""},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    assert not changed and "reason" in text

    text, changed = chat._acceptance_handle(
        s, {"action": "revise", "id": "a1", "text": "a.txt 里有 world",
            "expect": {"contains": "world"}, "reason": "需求改成 world"},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    assert changed and "已更新" in text
    c = chat._acceptance_criteria(s)[0]
    assert c["text"] == "a.txt 里有 world"
    assert c["status"] == "pending"          # 改过之后必须重新验
    assert s["acceptance"]["history"][-1]["reason"] == "需求改成 world"


def test_revise_drop_removes_stale_item(tmp_path):
    s = {}
    chat._acceptance_handle(
        s, {"action": "set", "criteria": [_criterion(), _criterion("b.txt 有 hi")]},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    text, changed = chat._acceptance_handle(
        s, {"action": "revise", "id": "a2", "drop": True, "reason": "页面已不需要"},
        str(tmp_path), {"sid": "s1"}, agent_on=True)
    assert changed and "已移除" in text
    assert [c["id"] for c in chat._acceptance_criteria(s)] == ["a1"]
    assert chat._acceptance_handle(
        s, {"action": "revise", "id": "nope", "reason": "x"},
        str(tmp_path), {"sid": "s1"}, agent_on=True)[1] is False


def test_acceptance_nudge_lists_pending():
    text = chat._acceptance_nudge_msg([
        {"id": "a1", "status": "pending"},
        {"id": "a2", "status": "failed"}])
    assert "2 条" in text and "a1" in text and "a2" in text
    assert "acceptance" in text


def test_acceptance_reads_mesh_metrics():
    import json

    text = "__MESH_METRICS__ " + json.dumps(
        {"triangles": 120, "degenerate": 0, "missing_textures": 0})
    ok, why = chat._acceptance_expect(
        {"triangles_min": 100, "degenerate_max": 0}, text)
    assert ok, why
    ok2, why2 = chat._acceptance_expect({"triangles_min": 500}, text)
    assert not ok2 and "triangles" in why2
