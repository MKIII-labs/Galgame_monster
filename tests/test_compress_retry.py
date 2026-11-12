# -*- coding: utf-8 -*-
"""压缩兜底：PTL 重试与连续失败熔断（对齐 Claude Code）。"""
import types

import pytest

from app import context, llm


class _HTTPLikeError(Exception):
    def __init__(self, body):
        super().__init__("400 Client Error")
        self.response = types.SimpleNamespace(text=body, status_code=400)


@pytest.fixture(autouse=True)
def _clean_breaker():
    context._COMPRESS_FAILS.clear()
    yield
    context._COMPRESS_FAILS.clear()


def test_ptl_retry_drops_oldest_and_succeeds(monkeypatch):
    calls = []

    def fake(*a, **kw):
        calls.append(len(a[3]))          # a[3] = messages
        if len(calls) < 3:
            raise _HTTPLikeError("This model's maximum context length is 8192 tokens")
        return "## 概要\n- 完成"

    monkeypatch.setattr(llm, "_quick_chat", fake)
    replay = [{"role": "user", "content": "第 %d 条" % i} for i in range(10)]
    out = context._api_compress("k", "u", "m", "sys", ["pin"], replay, 256, None)
    assert out == "## 概要\n- 完成"
    assert len(calls) == 3
    assert calls[0] > calls[1] > calls[2]     # 每次都丢掉最旧的一段


def test_non_overflow_error_is_not_retried(monkeypatch):
    calls = []

    def fake(*a, **kw):
        calls.append(1)
        raise RuntimeError("connection reset")

    monkeypatch.setattr(llm, "_quick_chat", fake)
    out = context._api_compress("k", "u", "m", "", [], [{"role": "user", "content": "x"}],
                                256, None)
    assert out == ""
    assert len(calls) == 1


def test_breaker_opens_after_three_failures():
    sid = "s_break"
    for _ in range(context.COMPRESS_BREAKER_FAILS):
        context._note_compress_result(sid, False)
    assert context._compress_blocked(sid) is True
    context._note_compress_result(sid, True)
    assert context._compress_blocked(sid) is False
