# -*- coding: utf-8 -*-
"""事件流为真相源：启动对账、删除不复活、json 模式回退。

会话**列表**仍以 sessions.json 为准（删除不会从事件流复活）；
每个会话的**内容**取「JSON 与事件流里 updated_at 较新的那份」。
"""
import json

import pytest

from app import session_events as se
from app import sessions
from app.store import drop_cache


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


def test_merge_failure_does_not_latch(env, monkeypatch):
    """对账抛异常时必须回滚 _MERGED：否则一次瞬时 IO 失败会让本进程永久跳过对账。"""
    sid = "s4"
    _write(env / "sessions.json", {"current": sid, "sessions": [_sess(sid, "t", 1.0)]})
    monkeypatch.setattr(sessions, "_store_mode", lambda: "events")

    def boom(_data, skip=None):
        raise OSError("事件流读盘失败")

    monkeypatch.setattr(sessions, "_merge_events_once", boom)
    with pytest.raises(OSError):
        sessions.load_sessions()
    assert sessions._MERGED is False          # 失败不置位

    monkeypatch.setattr(sessions, "_merge_events_once", lambda _data, skip=None: None)
    sessions.load_sessions()                  # 第二次会重试
    assert sessions._MERGED is True


# ---------------------------------------------------------------------------
# 单会话快照（热路径落库）
# ---------------------------------------------------------------------------

def test_hot_save_writes_only_that_session(env, monkeypatch):
    """热落库只写 session_store/ 里的单会话快照，不重写整份 sessions.json。"""
    sid, other_id = "s5", "s6"
    s = _sess(sid, "热", 1.0, [{"role": "user", "content": "a"}])
    other = _sess(other_id, "别的", 1.0, [{"role": "user", "content": "x"}])
    _write(env / "sessions.json",
           {"current": sid, "sessions": [s, other], "saved_at": 1.0})
    monkeypatch.setattr(sessions, "_store_mode", lambda: "json")

    data = sessions.load_sessions()
    target = sessions.find_session(data, sid)
    target["messages"].append({"role": "assistant", "content": "新回答"})
    target["updated_at"] = 2.0

    before = (env / "sessions.json").stat().st_mtime_ns
    sessions.save_session(target)
    assert (env / "sessions.json").stat().st_mtime_ns == before     # 整份没动
    snap = env / "session_store" / (sid + ".json")
    assert snap.is_file()

    # 模拟进程重启：整份还是旧的，单会话快照要能把这一条恢复回来
    drop_cache(str(env / "sessions.json"))
    sessions.reset_merge_flag()
    se._LAST.clear()
    data2 = sessions.load_sessions()
    got = sessions.find_session(data2, sid)
    assert got["messages"][-1]["content"] == "新回答"
    assert sessions.find_session(data2, other_id)["messages"][0]["content"] == "x"


def test_stale_store_snapshot_is_discarded(env, monkeypatch):
    """比 sessions.json 旧的单会话快照不能覆盖它，而且要顺手清掉。"""
    sid = "s7"
    _write(env / "sessions.json",
           {"current": sid, "sessions": [_sess(sid, "整份最新", 3.0, [])], "saved_at": 3.0})
    monkeypatch.setattr(sessions, "_store_mode", lambda: "json")
    d = env / "session_store"
    d.mkdir()
    stale = d / (sid + ".json")
    stale.write_text(json.dumps({"saved_at": 1.0,
                                 "session": _sess(sid, "旧快照", 1.0, [])},
                                ensure_ascii=False), encoding="utf-8")

    drop_cache(str(env / "sessions.json"))
    sessions.reset_merge_flag()
    se._LAST.clear()
    data = sessions.load_sessions()
    assert data["sessions"][0]["title"] == "整份最新"
    assert not stale.exists()                 # 过期快照被清理


def test_full_save_stamps_saved_at(env):
    """整份落盘要带 saved_at：启动时用它判断单会话快照是否更新。"""
    sid = "s8"
    _write(env / "sessions.json", {"current": sid, "sessions": [_sess(sid, "t", 1.0, [])]})
    data = sessions.load_sessions()
    assert not data.get("saved_at")
    sessions.save_sessions(data)
    on_disk = json.loads((env / "sessions.json").read_text(encoding="utf-8"))
    assert on_disk["saved_at"] == data["saved_at"] > 0
    # 先记事件、后写快照：事件时间戳不会晚于 saved_at，启动时不会重复重放
    assert se.latest_ts(sid, str(env)) <= on_disk["saved_at"]
