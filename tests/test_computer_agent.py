# -*- coding: utf-8 -*-
"""Computer Use · 工具注册 / 审批预判（不启动浏览器）"""
from app import agent, computer


def test_tools_registered():
    for name in computer.TOOLS:
        assert name in agent.TOOLS
        assert name in agent._HANDLERS
        assert name not in agent.WRITE_TOOLS
        assert name not in agent.RUN_TOOLS     # 走自己的 judge_computer


def test_schema_gated_by_computer_on():
    off = [t["function"]["name"] for t in agent.native_tool_schemas(agent_on=True)]
    on = [t["function"]["name"] for t in agent.native_tool_schemas(agent_on=True,
                                                                  computer_on=True)]
    assert "computer_open" not in off
    assert "computer_open" in on and "computer_act" in on
    # 6 个，不多不少（schema 预算）：浏览器 5 + 桌面窗口列表 1
    assert len([x for x in on if x.startswith("computer_")]) == 6
    assert "computer_windows" in on


def test_aliases_fold_to_computer_tools():
    assert agent.normalize_tool_name("browser_open") == "computer_open"
    assert agent.normalize_tool_name("browser_click") == "computer_act"
    assert agent.normalize_tool_name("browser_screenshot") == "computer_shot"


def test_describe_call():
    d = agent.describe_call("computer_open", {"url": "https://example.com"})
    assert "example.com" in d["text"]
    d = agent.describe_call("computer_act", {"action": "type", "ref": 3, "value": "abc"})
    assert "type" in d["text"] and "[3]" in d["text"] and "3 字" in d["text"]


def test_judge_computer_blocks_sensitive(monkeypatch):
    monkeypatch.setattr(computer, "enabled", lambda: True)
    decision, why, host = agent.judge_computer("computer_open", {"url": "https://mail.qq.com/"})
    assert decision == "deny" and "内置禁区" in why


def test_judge_computer_asks_unknown_site(monkeypatch):
    monkeypatch.setattr(computer, "enabled", lambda: True)
    monkeypatch.setattr(computer.policy, "_cfg", lambda: {"computer_sites": []})
    decision, why, host = agent.judge_computer("computer_open", {"url": "https://example.com/x"})
    assert decision == "ask" and host == "example.com"


def test_judge_computer_disabled(monkeypatch):
    monkeypatch.setattr(computer, "enabled", lambda: False)
    assert agent.judge_computer("computer_open", {"url": "https://example.com"})[0] == "deny"


def test_dispatch_when_disabled(monkeypatch):
    monkeypatch.setattr(computer, "enabled", lambda: False)
    text = computer.dispatch("computer_see", {})
    assert "未开启" in text
