# -*- coding: utf-8 -*-
"""大厅提示词开关：股票 / B 站说明可关，日历 / 日程不受影响。"""
from app import agent, capabilities, prompt_assembly, scenes


def _fake_lines(chat_only=True):
    if chat_only:
        return ["- calendar.create：建日程"]
    return ["- calendar.create：建日程", "- stock.brief：看股市",
            "- bili.analyze：看 B 站"]


def test_capability_prompt_hall_off(monkeypatch):
    monkeypatch.setattr(capabilities, "describe_lines", _fake_lines)
    monkeypatch.setattr(scenes, "filter_caps", lambda scene, ids: list(ids))
    p = agent._capability_prompt({}, compact=False, domains=None, hall=False)
    assert "calendar.create" in p
    assert "stock." not in p
    assert "bili." not in p
    assert "模拟股市" not in p


def test_capability_prompt_hall_on(monkeypatch):
    monkeypatch.setattr(capabilities, "describe_lines", _fake_lines)
    monkeypatch.setattr(scenes, "filter_caps", lambda scene, ids: list(ids))
    p = agent._capability_prompt({}, compact=False, domains=None, hall=True)
    assert "stock.brief" in p
    assert "bili.analyze" in p
    assert "模拟股市" in p


def test_tool_decision_hall_off():
    p = agent._tool_decision_prompt(file_ready=True, search_ready=True, hall=False)
    assert "模拟股市" not in p
    assert "B 站" not in p
    assert "记忆" in p


def test_ui_context_hall_off():
    raw = {"mode": "hall", "module": "stock", "game_id": "g1",
           "cursor_date": "2026-01-01", "symbol": "AAPL"}
    assert prompt_assembly.ui_context_line(raw, hall=False) == ""
    assert "模拟股市" in prompt_assembly.ui_context_line(raw, hall=True)


def test_build_prompt_hall_switch(monkeypatch):
    monkeypatch.setattr("app.config.load_config",
                        lambda: {"agent_hall_prompt": False})
    p = agent.build_agent_prompt({}, search_ready=False)
    assert "模拟股市" not in p
    assert "B 站" not in p

    monkeypatch.setattr("app.config.load_config",
                        lambda: {"agent_hall_prompt": True})
    p2 = agent.build_agent_prompt({}, search_ready=False)
    assert "模拟股市" in p2

def test_capability_boundary_points_to_tool_search_when_hidden():
    """有未挂载组时，能力边界必须先让模型去 tool_search 解锁，而不是直接说「不可用」。

    回归：旧文案是「清单里没有 = 当前不可用，直接说不可用」，和同一份提示词里的
    「低频组需要先 tool_search 解锁」打架，模型通常选绝对那条。
    """
    from app.agent.prompt import capability_boundary

    with_hidden = capability_boundary(True)
    assert "tool_search" in with_hidden
    assert "不要直接说" in with_hidden
    plain = capability_boundary(False)
    assert "tool_search" not in plain

def test_prompt_tells_user_how_to_enable_file_tools():
    """P2：没有文件工具时，要给出「怎么开启」的下一步，不能只说「我没有能力」。"""
    plain = agent.build_agent_prompt({}, search_ready=False)
    assert "切到 Agent" in plain
    no_sandbox = agent.build_agent_prompt({"agent_enabled": True}, search_ready=False)
    assert "📁" in no_sandbox

def test_base_principles_come_first_and_forbid_report_files():
    """base 层：通用原则必须排在所有提示词最前，且明确「不要为有产物而建文件」。"""
    p = agent.build_agent_prompt({"agent_enabled": True, "sandbox": "."}, search_ready=False)
    assert p.startswith("## 基本原则"), p[:40]
    assert "不要为了「有个产物」而新建文件" in p
    assert "能改现有文件就不要新建" in p
