# -*- coding: utf-8 -*-
"""L2 会话工作记忆：触发条件、后台摘要、压缩零 API 复用。"""
import time

import pytest

from app import context, llm, session_memory


def _msgs(n, per_chars=500, tools=0):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": "你" * per_chars + str(i)})
        m = {"role": "assistant", "content": "好" * per_chars + str(i)}
        if tools:
            m["tools"] = [{"tool": "read_file", "args": {"path": "a.py"},
                           "result": "ok"}] * tools
        out.append(m)
    return out


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(session_memory, "load_config",
                        lambda: {"session_memory_enabled": True})
    yield


def test_initial_update_needs_enough_tokens():
    small = {"messages": _msgs(2, per_chars=20)}
    assert session_memory.should_update(small, had_tools=False) is False
    big = {"messages": _msgs(20, per_chars=500)}
    assert session_memory.should_update(big, had_tools=False) is True


def test_update_needs_token_growth_and_a_second_signal():
    msgs = _msgs(20, per_chars=500)
    total = context.estimate_messages_tokens(msgs)
    base = {"messages": msgs, "session_memory": "## 目标\n- x" * 30,
            "session_memory_upto": len(msgs), "session_memory_tokens": total - 1000}
    assert session_memory.should_update(base, had_tools=False) is False   # 增长不够

    base["session_memory_tokens"] = total - 7000
    assert session_memory.should_update(base, had_tools=False) is True    # 自然停顿
    assert session_memory.should_update(base, had_tools=True) is False    # 有工具但不够 3 次

    msgs2 = msgs + [{"role": "assistant", "content": "继续",
                     "tools": [{"tool": "read_file"}] * 3}]
    base["messages"] = msgs2
    base["session_memory_upto"] = len(msgs)
    assert session_memory.should_update(base, had_tools=True) is True


def test_build_update_writes_fields(monkeypatch):
    monkeypatch.setattr(session_memory, "get_api_conf", lambda *a, **kw: ("k", "u", "m"))
    monkeypatch.setattr(llm, "_quick_chat",
                        lambda *a, **kw: "## 目标\n- 做 X\n" + ("内容" * 100))
    s = {"id": "s1", "messages": _msgs(12, per_chars=200),
         "session_memory_upto": 0}
    fields = session_memory.build_update(s)
    assert fields["session_memory"].startswith("## 目标")
    assert fields["session_memory_upto"] == len(s["messages"])
    assert fields["session_memory_tokens"] > 0
    assert fields["session_memory_fp"]


def test_light_failure_falls_back_to_main_model(monkeypatch):
    monkeypatch.setattr(session_memory, "load_config",
                        lambda: {"session_memory_enabled": True, "light_model": "small"})
    monkeypatch.setattr(session_memory, "get_api_conf", lambda *a, **kw: ("k", "u", "m"))
    calls = []

    def fake(*a, **kw):
        calls.append(kw.get("use_light"))
        if kw.get("use_light"):
            raise RuntimeError("light down")
        return "## 目标\n- 主模型兜底\n" + ("内容" * 100)

    monkeypatch.setattr(llm, "_quick_chat", fake)
    fields = session_memory.build_update({"id": "s", "messages": _msgs(4, per_chars=100)})
    assert calls == [True, False]
    assert fields["session_memory"].startswith("## 目标")


def test_missing_light_model_calls_main_only_once(monkeypatch):
    monkeypatch.setattr(session_memory, "load_config",
                        lambda: {"session_memory_enabled": True})
    monkeypatch.setattr(session_memory, "get_api_conf", lambda *a, **kw: ("k", "u", "m"))
    calls = []

    def fake(*a, **kw):
        calls.append(kw.get("use_light"))
        return "## 目标\n- 主模型\n" + ("内容" * 100)

    monkeypatch.setattr(llm, "_quick_chat", fake)
    session_memory.build_update({"id": "s", "messages": _msgs(4, per_chars=100)})
    assert calls == [True]                # 没配轻量模型时，第一次本来就是主模型


def test_compress_uses_session_memory_without_api(monkeypatch):
    msgs = _msgs(10, per_chars=100)
    s = {"id": "s_sm", "messages": msgs, "summary_upto": 0,
         "session_memory": "## 目标\n- 已经做过的事\n" * 20,
         "session_memory_upto": len(msgs),
         "session_memory_at": time.time(),
         "session_memory_fp": context.range_fingerprint(msgs, len(msgs))}
    monkeypatch.setattr(context, "load_config",
                        lambda: {"chat_context_limit": 100000,
                                 "chat_reserve_output": 1000,
                                 "history_working_set_tokens": 0,
                                 "token_profile": "quality"})
    monkeypatch.setattr(context, "context_limits", lambda: (100000, 1000))
    monkeypatch.setattr(context, "keep_from_by_tokens", lambda *a, **kw: 4)
    called = []
    monkeypatch.setattr(llm, "_quick_chat", lambda *a, **kw: called.append(1) or "x")

    assert context.compress_history(s, "") is True
    assert called == []                       # 零 API
    assert s["summary"].startswith("## 目标")
    assert s["summary_upto"] == 4
    assert s["compress_marks"]


def test_compress_falls_back_when_session_memory_fingerprint_mismatch(monkeypatch):
    msgs = _msgs(10, per_chars=100)
    s = {"id": "s_sm2", "messages": msgs, "summary_upto": 0,
         "session_memory": "## 目标\n- 旧结论\n" * 20,
         "session_memory_upto": len(msgs),
         "session_memory_at": time.time(),
         "session_memory_fp": "stale"}
    monkeypatch.setattr(context, "load_config",
                        lambda: {"chat_context_limit": 100000,
                                 "chat_reserve_output": 1000,
                                 "history_working_set_tokens": 0,
                                 "token_profile": "quality"})
    monkeypatch.setattr(context, "context_limits", lambda: (100000, 1000))
    monkeypatch.setattr(context, "get_api_conf", lambda *a, **kw: ("k", "u", "m"))
    monkeypatch.setattr(context, "keep_from_by_tokens", lambda *a, **kw: 4)
    called = []
    monkeypatch.setattr(llm, "_quick_chat",
                        lambda *a, **kw: called.append(1) or "## 新摘要\n- 重新压")

    assert context.compress_history(s, "") is True
    assert called                              # 指纹不符：必须重新摘要
    assert s["summary"].startswith("## 新摘要")
