# -*- coding: utf-8 -*-
"""Reactive Compact：413 时保留最后 4 条，其余紧急摘要。"""
import pytest

from app import context, llm


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(context, "get_api_conf", lambda *a, **kw: ("k", "u", "m"))
    yield


def _messages():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(8):
        msgs.append({"role": "user", "content": "用户问题 %d " % i + "x" * 400})
        msgs.append({"role": "assistant", "content": "助手回答 %d " % i + "y" * 400})
    return msgs


def test_reactive_keeps_last_four(monkeypatch):
    monkeypatch.setattr(llm, "_quick_chat",
                        lambda *a, **kw: "## 紧急摘要\n- 关键结论")
    msgs = _messages()
    out, dropped = context.reactive_compact(msgs, keep_last=4)
    assert dropped == len(msgs) - 1 - 4
    assert len(out) == 1 + 1 + 4
    assert out[0]["role"] == "system"
    assert "【上下文超窗" in out[1]["content"]
    assert "紧急摘要" in out[1]["content"]
    assert out[-1]["content"].startswith("助手回答 7")


def test_reactive_keeps_tool_pair(monkeypatch):
    monkeypatch.setattr(llm, "_quick_chat",
                        lambda *a, **kw: "## 紧急摘要\n- 工具结论")
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(6):
        msgs.append({"role": "user", "content": "问题 %d " % i + "x" * 400})
        msgs.append({"role": "assistant", "content": "回答 %d " % i + "y" * 400})
    msgs.append({"role": "assistant", "content": "调用工具",
                 "tools": [{"tool": "read_file"}]})
    for i in range(3):
        msgs.append({"role": "user",
                     "content": context.TOOL_MSG_PREFIX + "\n结果 %d " % i + "z" * 400})
    out, dropped = context.reactive_compact(msgs, keep_last=4)
    assert dropped > 0
    # tail 不能从工具结果开头：必须带上对应的 assistant
    assert out[2].get("role") == "assistant"
    assert "调用工具" in out[2].get("content")


def test_reactive_no_key_returns_original(monkeypatch):
    monkeypatch.setattr(context, "get_api_conf", lambda *a, **kw: ("", "", ""))
    msgs = _messages()
    out, dropped = context.reactive_compact(msgs, keep_last=4)
    assert dropped == 0
    assert out is msgs
