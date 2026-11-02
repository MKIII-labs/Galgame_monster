# -*- coding: utf-8 -*-
"""计划卡与聊天落库 / 工具层的协同：续写回写、caps 回调、缺上下文时的提示。"""
from app import agent
from app.api import chat


def test_carry_plan_only_when_steps():
    msg = {}
    chat._carry_plan(msg, {"steps": []})
    assert "plan" not in msg

    plan = {"revision": 2, "steps": [{"id": "S1", "text": "a", "status": "doing"}]}
    chat._carry_plan(msg, plan)
    assert msg["plan"] is plan

    msg2 = {}
    chat._carry_plan(msg2, "不是计划")
    assert msg2 == {}

    msg3 = {"plan": {"revision": 1, "steps": [{"id": "S1"}]}}
    chat._carry_plan(msg3, None)
    assert msg3["plan"]["revision"] == 1        # 没有新计划时保留旧的


def test_plan_tool_routes_through_caps():
    calls = []

    def fake(name, args):
        calls.append((name, args))
        return True, "ok:%s" % name

    text, undo, write = agent.run_tool("", "update_plan", {"steps": ["a"]},
                                       caps={"plan_tool": fake})
    assert text == "ok:update_plan" and calls == [("update_plan", {"steps": ["a"]})]
    assert undo is None and write is False

    text2, _, _ = agent.run_tool("", "plan_patch", {"reason": "x"},
                                 caps={"plan_tool": fake})
    assert text2 == "ok:plan_patch"


def test_plan_tool_without_session_context_says_so():
    text, _, _ = agent.run_tool("", "update_plan", {"steps": ["a"]})
    assert text.startswith("❌") and "对话会话" in text


def test_acceptance_routes_through_caps():
    text, undo, write = agent.run_tool(
        "", "acceptance", {"action": "status"},
        caps={"acceptance_tool": lambda a: (True, "验收：无")})
    assert text == "验收：无" and undo is None and write is False

    text2, _, _ = agent.run_tool("", "acceptance", {"action": "status"})
    assert text2.startswith("❌") and "对话会话" in text2
