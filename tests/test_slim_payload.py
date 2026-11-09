# -*- coding: utf-8 -*-
"""前端消息窗口：done / state 只回最近 N 条，更早的按需拉取。"""

from app import sessions


def _data(n=100):
    msgs = [{"role": "user", "content": "m%d" % i} for i in range(n)]
    return {"current": "s1", "sessions": [
        {"id": "s1", "title": "当前会话", "messages": msgs, "updated_at": 1},
        {"id": "s2", "title": "其它会话", "messages": msgs[:5], "updated_at": 2},
    ]}


def _cur(out):
    return [s for s in out["sessions"] if s["id"] == "s1"][0]


def test_window_comes_from_config(monkeypatch):
    monkeypatch.setattr("app.config.load_config",
                        lambda: {"chat_payload_msg_window": 5})
    cur = _cur(sessions.slim_sessions(_data()))
    assert len(cur["messages"]) == 5
    assert cur["msg_total"] == 100
    assert cur["msg_start"] == 95


def test_explicit_limit_wins(monkeypatch):
    monkeypatch.setattr("app.config.load_config",
                        lambda: {"chat_payload_msg_window": 5})
    cur = _cur(sessions.slim_sessions(_data(), msg_limit=10))
    assert len(cur["messages"]) == 10
    assert cur["msg_start"] == 90


def test_zero_window_means_all(monkeypatch):
    monkeypatch.setattr("app.config.load_config",
                        lambda: {"chat_payload_msg_window": 0})
    cur = _cur(sessions.slim_sessions(_data()))
    assert len(cur["messages"]) == 100
    assert cur["msg_start"] == 0


def test_missing_config_uses_default(monkeypatch):
    monkeypatch.setattr("app.config.load_config", lambda: {})
    cur = _cur(sessions.slim_sessions(_data()))
    assert len(cur["messages"]) == sessions.MSG_WINDOW


def test_other_sessions_have_no_messages(monkeypatch):
    monkeypatch.setattr("app.config.load_config",
                        lambda: {"chat_payload_msg_window": 5})
    out = sessions.slim_sessions(_data())
    other = [s for s in out["sessions"] if s["id"] == "s2"][0]
    assert other["messages"] is None
    assert other["msg_total"] == 5
