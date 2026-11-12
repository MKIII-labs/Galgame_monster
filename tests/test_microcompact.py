# -*- coding: utf-8 -*-
"""L3 微压缩：只压单条过大的工具结果，保留最近 2 条，按原文缓存。"""
import pytest

from app import context, llm


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    context._MICRO_CACHE.clear()
    monkeypatch.setattr(context, "load_config", lambda: {
        "microcompact_enabled": True,
        "microcompact_min_tokens": 3000,
        "microcompact_trigger_ratio": 0.6,
    })
    monkeypatch.setattr(context, "light_chat_is_local", lambda cfg=None: False)
    monkeypatch.setattr(context, "get_api_conf", lambda *a, **kw: ("k", "u", "m"))
    yield
    context._MICRO_CACHE.clear()


def _messages():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(5):
        msgs.append({"role": "user",
                     "content": context.TOOL_MSG_PREFIX + "\n" + ("x" * 12000 + str(i))})
    return msgs


def test_microcompact_only_old_large_tool_results(monkeypatch):
    calls = []

    def fake(*a, **kw):
        calls.append(1)
        return "错误 E1；文件 a.py:12；命令 pytest"

    monkeypatch.setattr(llm, "_quick_chat", fake)
    msgs = _messages()
    out, done = context.microcompact_messages(msgs, 20000)
    assert done == 3
    assert out[1]["content"].startswith(context.TOOL_MSG_PREFIX)
    assert "【微压缩】" in out[1]["content"]
    assert "【微压缩】" in out[3]["content"]
    assert "【微压缩】" not in out[4]["content"]      # 最近 2 条保留
    assert "【微压缩】" not in out[5]["content"]

    # 同样内容再跑一次：命中缓存，不再调模型
    context.microcompact_messages(_messages(), 20000)
    assert len(calls) == 3


def test_microcompact_below_trigger_does_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_quick_chat", lambda *a, **kw: calls.append(1) or "x")
    msgs = _messages()
    out, done = context.microcompact_messages(msgs, 100000)
    assert done == 0
    assert calls == []
    assert out is msgs


def test_microcompact_disabled(monkeypatch):
    monkeypatch.setattr(context, "load_config", lambda: {"microcompact_enabled": False})
    calls = []
    monkeypatch.setattr(llm, "_quick_chat", lambda *a, **kw: calls.append(1) or "x")
    out, done = context.microcompact_messages(_messages(), 20000)
    assert done == 0 and calls == []
    assert out[1]["content"].startswith(context.TOOL_MSG_PREFIX)
    assert "【微压缩】" not in out[1]["content"]
