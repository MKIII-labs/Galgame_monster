# -*- coding: utf-8 -*-
"""事件流为真相源：启动对账、删除不复活、json 模式回退。

会话**列表**仍以 sessions.json 为准（删除不会从事件流复活）；
每个会话的**内容**取「JSON 与事件流里 updated_at 较新的那份」。
"""
import json

import pytest

from app import session_events as se
from app import sessions


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(se, "DATA_DIR", str(tmp_path))
    sessions.reset_merge_flag()
    se._LAST.clear()
    yield tmp_path
    sessions.reset_merge_flag()
    se._LAST.clear()


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _sess(sid, title, ts, msgs=None):
    return {"id": sid, "title": title, "updated_at": ts,
            "messages": msgs if msgs is not None else []}


def test_events_win_when_json_behind(env, monkeypatch):
    sid = "s1"
    old = _sess(sid, "旧", 1.0, [{"role": "user", "content": "旧内容"}])
    _write(env / "sessions.json", {"current": sid, "sessions": [old]})
    new = _sess(sid, "新", 2.0, [{"role": "user", "content": "旧内容"},
                                 {"role": "assistant", "content": "新回答"}])
    se.record(old, str(env))            # 基线
    se.record(new, str(env))            # 变化

    monkeypatch.setattr(sessions, "_store_mode", lambda: "events")
    data = sessions.load_sessions()
    s = data["sessions"][0]
    assert s["updated_at"] == 2.0
    assert s["messages"][-1]["content"] == "新回答"
    # 对账后 JSON 也被回写成最新，避免下次重启重复比较
    on_disk = json.loads((env / "sessions.json").read_text(encoding="utf-8"))
    assert on_disk["sessions"][0]["updated_at"] == 2.0


def test_json_newer_wins(env, monkeypatch):
    sid = "s2"
    old = _sess(sid, "旧", 1.0, [{"role": "user", "content": "a"}])
    se.record(old, str(env))
    newer = _sess(sid, "JSON 更新", 3.0, [{"role": "user", "content": "b"}])
    _write(env / "sessions.json", {"current": sid, "sessions": [newer]})

    monkeypatch.setattr(sessions, "_store_mode", lambda: "events")
    data = sessions.load_sessions()
    assert data["sessions"][0]["title"] == "JSON 更新"
    assert data["sessions"][0]["messages"][0]["content"] == "b"


def test_deleted_session_does_not_revive(env, monkeypatch):
    dead = _sess("s_dead", "该删", 1.0, [])
    se.record(dead, str(env))           # 事件流里有它
    other = _sess("s_keep", "保留", 1.0, [])
    _write(env / "sessions.json", {"current": "s_keep", "sessions": [other]})

    monkeypatch.setattr(sessions, "_store_mode", lambda: "events")
    data = sessions.load_sessions()
    assert [s["id"] for s in data["sessions"]] == ["s_keep"]


def test_json_mode_skips_merge(env, monkeypatch):
    sid = "s3"
    old = _sess(sid, "旧", 1.0, [{"role": "user", "content": "old"}])
    _write(env / "sessions.json", {"current": sid, "sessions": [old]})
    new = _sess(sid, "新", 2.0, [{"role": "user", "content": "new"}])
    se.record(old, str(env))
    se.record(new, str(env))

    monkeypatch.setattr(sessions, "_store_mode", lambda: "json")
    data = sessions.load_sessions()
    assert data["sessions"][0]["updated_at"] == 1.0
    assert data["sessions"][0]["messages"][0]["content"] == "old"
