# -*- coding: utf-8 -*-
"""轻量 LLM 调用：响应缓存与过期。"""
from app import llm


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def _clear():
    with llm._QUICK_CACHE_LOCK:
        llm._QUICK_CACHE.clear()


def test_quick_chat_reuses_cached_response(monkeypatch):
    _clear()
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(llm, "_post_retry", fake_post)
    monkeypatch.setattr(llm, "_record_quick_usage", lambda *a, **k: None)
    msgs = [{"role": "user", "content": "同一个问题"}]
    first = llm._quick_chat("k", "http://x/v1", "m", msgs, max_tokens=20)
    second = llm._quick_chat("k", "http://x/v1", "m", msgs, max_tokens=20)
    assert first == second == "ok"
    assert len(calls) == 1


def test_quick_chat_cache_expires():
    _clear()
    key = llm._quick_cache_key("http://x/v1", "m", [{"role": "user", "content": "q"}], 20)
    llm._quick_cache_put(key, "old")
    assert llm._quick_cache_get(key) == "old"
    with llm._QUICK_CACHE_LOCK:
        ts, text = llm._QUICK_CACHE[key]
        llm._QUICK_CACHE[key] = (ts - llm._QUICK_CACHE_TTL - 1, text)
    assert llm._quick_cache_get(key) is None
    _clear()


def test_quick_chat_different_max_tokens_is_not_reused(monkeypatch):
    _clear()
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(llm, "_post_retry", fake_post)
    monkeypatch.setattr(llm, "_record_quick_usage", lambda *a, **k: None)
    msgs = [{"role": "user", "content": "同一个问题"}]
    llm._quick_chat("k", "http://x/v1", "m", msgs, max_tokens=20)
    llm._quick_chat("k", "http://x/v1", "m", msgs, max_tokens=40)
    assert len(calls) == 2
    _clear()
