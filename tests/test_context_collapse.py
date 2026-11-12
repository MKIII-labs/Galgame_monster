# -*- coding: utf-8 -*-
"""L4 渐进折叠：窗口预算 80% 先轻压一档，100% 才完整压缩。"""
import pytest

from app import context


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    s = {"id": "s1", "messages": [], "ai_write": False}
    monkeypatch.setattr(context, "begin_compress", lambda sid: True)
    monkeypatch.setattr(context, "end_compress", lambda sid: None)
    monkeypatch.setattr(context, "compress_stop_event", lambda sid: None)
    monkeypatch.setattr(context, "_compress_blocked", lambda sid: False)
    monkeypatch.setattr(context, "load_sessions", lambda: {"sessions": [s]})
    monkeypatch.setattr(context, "find_session", lambda data, sid: s)
    monkeypatch.setattr(context, "request_budget", lambda: 100000)
    monkeypatch.setattr(context, "load_config", lambda: {
        "history_collapse_ratio": 0.8,
        "history_collapse_keep_ratio": 0.32,
    })
    monkeypatch.setattr(context, "save_summary", lambda sid, s: s)
    monkeypatch.setattr(context, "flush_summary_memory", lambda sid: 0)
    ctx = {"used": 1000, "limit": 100000, "remain": 99000, "pct": 1.0,
           "parts": {"system": 0, "image": 0}}
    monkeypatch.setattr(context, "session_context", lambda s, prompt="": ctx)
    yield monkeypatch


def _run(monkeypatch, used, fake_used=None):
    calls = []

    def fake_compress(s, system_prompt="", stop_event=None, keep_ratio=None, stage="auto"):
        calls.append({"keep_ratio": keep_ratio, "stage": stage})
        return True

    monkeypatch.setattr(context, "compress_history", fake_compress)
    ctx = {"used": used, "limit": 100000, "remain": 100000 - used, "pct": 1.0,
           "parts": {"system": 0, "image": 0}}
    monkeypatch.setattr(context, "session_context", lambda s, prompt="": ctx)
    assert context.maybe_precompress("s1", 0, 0, "", background=False) is True
    return calls


def test_collapse_at_eighty_percent(_env):
    calls = _run(_env, 85000)
    assert len(calls) == 1
    assert calls[0]["stage"] == "collapse"
    assert calls[0]["keep_ratio"] == 0.32


def test_auto_compact_at_full_budget(_env):
    calls = _run(_env, 100000)
    assert len(calls) == 1
    assert calls[0]["stage"] == "auto"
    assert calls[0]["keep_ratio"] is None


def test_no_compaction_below_collapse_ratio(_env):
    calls = _run(_env, 70000)
    assert calls == []
