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


def test_acceptance_gate_blocks_multistep_until_defined():
    state = {"blocked": 0, "granted": False}
    assert chat._acceptance_gate_action(state, "write_file", [], 3, 0) == "block"
    assert chat._acceptance_gate_action(state, "write_file", [], 3, 0) == "block"
    assert chat._acceptance_gate_action(state, "write_file", [], 3, 0) == "allow_after_block"

    assert chat._acceptance_gate_action(
        {"blocked": 0, "granted": False}, "write_file", [{"id": "a1"}], 3, 0) == "allow"
    assert chat._acceptance_gate_action(
        {"blocked": 0, "granted": False}, "write_file", [], 1, 0) == "allow"
    assert chat._acceptance_gate_action(
        {"blocked": 0, "granted": False}, "recover_file", [], 3, 5) == "allow"


def test_acceptance_nudge_lists_pending():
    text = chat._acceptance_nudge_msg([
        {"id": "a1", "status": "pending"},
        {"id": "a2", "status": "failed"}])
    assert "2 条" in text and "a1" in text and "a2" in text
    assert "acceptance" in text
