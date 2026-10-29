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
