# -*- coding: utf-8 -*-
"""切会话的负载裁剪：只回最近 N 条 + current 侧车文件。"""
from app import sessions


def _data(n=200):
    msgs = [{"role": "user", "content": str(i)} for i in range(n)]
    return {"current": "s1", "sessions": [
        {"id": "s1", "title": "大会话", "messages": msgs, "updated_at": 1},
        {"id": "s2", "title": "小会话", "messages": msgs[:5], "updated_at": 1},
    ]}


def test_slim_sessions_windows_current_messages():
    out = sessions.slim_sessions(_data(200), msg_limit=80)
    cur = next(x for x in out["sessions"] if x["id"] == "s1")
    assert cur["msg_total"] == 200
    assert cur["msg_start"] == 120
    assert len(cur["messages"]) == 80
    assert cur["messages"][0]["content"] == "120"
    # 非当前会话不传消息，但仍给出总数（前端列表/搜索用）
    other = next(x for x in out["sessions"] if x["id"] == "s2")
    assert other["messages"] is None
    assert other["msg_total"] == 5


def test_slim_sessions_keeps_short_history_intact():
    out = sessions.slim_sessions(_data(30), msg_limit=80)
    cur = next(x for x in out["sessions"] if x["id"] == "s1")
    assert cur["msg_start"] == 0
    assert len(cur["messages"]) == 30


def test_set_current_sidecar_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "CURRENT_FILE", str(tmp_path / "current.json"))
    data = {"current": "s1", "sessions": [
        {"id": "s1", "messages": []}, {"id": "s2", "messages": []},
    ]}
    sessions.set_current("s2")
    sessions._apply_current(data)
    assert data["current"] == "s2"

    # 侧车指向已删除的会话：不能把 current 改坏
    sessions.set_current("gone")
    data["current"] = "s1"
    sessions._apply_current(data)
    assert data["current"] == "s1"
