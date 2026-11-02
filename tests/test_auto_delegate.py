# -*- coding: utf-8 -*-
"""自动派发：suggest / auto-plan / auto-review 的纯逻辑部分。"""
import pytest

from app import agent_defs, auto_delegate, explore


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_defs, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(explore, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(explore, "VERIFY_EVIDENCE", False)


def _fake_chat(ctx, messages, schemas, stop_event, model=None):
    return {"visible": "结论：可以按三步做\n置信度: high；未查清: 无",
            "tool_calls": [], "usage": {"total_tokens": 3}}


def _ctx(tmp_path):
    return {"sandbox": str(tmp_path), "api_key": "k", "api_url": "u",
            "model": "m", "emit": lambda o: None, "budget": 0,
            "chat_fn": _fake_chat, "verify_evidence": False}


def test_plan_worthy_only_for_tasks():
    assert auto_delegate.plan_worthy("把通知模块重构一下，顺手补测试")
    assert auto_delegate.plan_worthy("接入一个图片生成接口并加设置页")
    # 继续 / 接着：沿用上一轮计划
    assert not auto_delegate.plan_worthy("继续做完剩下的部分")
    # 寒暄 / 纯问答 / 太短的指令
    assert not auto_delegate.plan_worthy("你好")
    assert not auto_delegate.plan_worthy("?这个文件在哪")
    assert not auto_delegate.plan_worthy("修复bug")


def test_routing_block_lists_agents():
    block = auto_delegate.routing_block()
    assert "reviewer" in block and "只读" in block
    assert "delegate" in block


def test_routing_block_empty_when_no_agent(monkeypatch):
    monkeypatch.setattr(auto_delegate, "enabled_agents", lambda: [])
    assert auto_delegate.routing_block() == ""


def test_route_hint_needs_read_streak():
    reads = [{"tool": "read_file", "write": False} for _ in range(3)]
    assert auto_delegate.route_hint(reads) == ""
    reads.append({"tool": "search_code", "write": False})
    assert "delegate" in auto_delegate.route_hint(reads)


def test_route_hint_cleared_by_write_or_delegate():
    rows = [{"tool": "read_file", "write": False} for _ in range(5)]
    assert auto_delegate.route_hint(rows + [{"tool": "write_file", "write": True}]) == ""
    assert auto_delegate.route_hint(rows + [{"tool": "delegate", "write": False}]) == ""


def test_run_agent_and_first_text(tmp_path):
    res = auto_delegate.run_agent(
        "planner", auto_delegate.plan_prompt("重构通知模块"), _ctx(tmp_path))
    assert res["ok"] is True
    assert auto_delegate.first_text(res).startswith("结论：")
    assert "自动规划" in auto_delegate.plan_note(auto_delegate.first_text(res))


def test_review_prompt_lists_writes():
    log = [{"tool": "write_file", "write": True, "args": {"path": "app/a.py"}},
           {"tool": "edit_file", "write": True, "args": {"path": "app/b.py"}},
           {"tool": "read_file", "write": False, "args": {"path": "app/c.py"}}]
    txt = auto_delegate.review_prompt("修个 bug", "改了两个文件", log)
    assert "app/a.py" in txt and "app/b.py" in txt
    assert "app/c.py" not in txt
    assert "可交付" in txt


def test_empty_notes():
    assert auto_delegate.plan_note("") == ""
    assert auto_delegate.review_note("") == ""
    assert auto_delegate.first_text({}) == ""
