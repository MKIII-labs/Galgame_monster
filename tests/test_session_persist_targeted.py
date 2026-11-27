# -*- coding: utf-8 -*-
"""会话持久化：元数据 / 列表操作只写目标会话，不再全量回写。

回归背景：改名、置顶、排序、删除原来会 load 整份快照再 save_sessions，
既重写所有会话缓存，也可能把并发追加的消息覆盖掉（重启后丢消息）。
"""
import threading
import time
from pathlib import Path

from app import sessions as store
from app.api import sessions as api


class _H:
    def __init__(self, body):
        self.body = body or {}
        self.data = None
        self.payload = None
        self.code = 200

    def _body(self):
        return self.body

    def _ok_sessions(self, data):
        self.data = store.slim_sessions(data)

    def _json(self, obj, code=200):
        self.code = code
        self.payload = obj
        return obj


def _seed(rows):
    store.save_sessions({"current": rows[0]["id"], "sessions": rows})
    store.reset_cache()


def _sess(sid, title=None, msgs=None):
    return {"id": sid, "title": title or sid, "updated_at": 1.0,
            "messages": list(msgs or [])}


def _mtime(sid):
    return Path(store._store_path(sid)).stat().st_mtime_ns


def test_rename_only_writes_target_session():
    _seed([_sess("tpa"), _sess("tpb")])
    before = _mtime("tpb")
    api._api_session_rename(_H({"id": "tpa", "title": "新标题"}))
    assert _mtime("tpb") == before            # 别的会话缓存没被重写
    store.reset_cache()
    a = store.find_session(store.load_sessions(), "tpa")
    assert a["title"] == "新标题" and a["title_source"] == "manual"


def test_pin_only_writes_target_session():
    _seed([_sess("tpa"), _sess("tpb")])
    before = _mtime("tpb")
    api._api_session_pin(_H({"id": "tpa", "pinned": True}))
    assert _mtime("tpb") == before
    store.reset_cache()
    a = store.find_session(store.load_sessions(), "tpa")
    assert a.get("pinned") is True


def test_reorder_writes_only_index():
    _seed([_sess("tpa"), _sess("tpb"), _sess("tpc")])
    before = {sid: _mtime(sid) for sid in ("tpa", "tpb", "tpc")}
    api._api_session_reorder(_H({"ids": ["tpc", "tpa", "tpb"]}))
    assert {sid: _mtime(sid) for sid in before} == before
    store.reset_cache()
    ids = [s["id"] for s in store.load_sessions()["sessions"]]
    assert ids == ["tpc", "tpa", "tpb"]


def test_delete_keeps_other_cache_untouched():
    _seed([_sess("tpa"), _sess("tpb")])
    before = _mtime("tpa")
    api._api_session_delete(_H({"id": "tpb"}))
    assert _mtime("tpa") == before
    store.reset_cache()
    ids = [s["id"] for s in store.load_sessions()["sessions"]]
    assert ids == ["tpa"]


def test_new_session_does_not_rewrite_existing():
    _seed([_sess("tpa")])
    before = _mtime("tpa")
    api._api_session_new(_H({"title": "新会话"}))
    assert _mtime("tpa") == before
    store.reset_cache()
    rows = store.load_sessions()["sessions"]
    assert len(rows) == 2 and rows[0]["title"] == "新会话"


def test_concurrent_append_survives_rename():
    """改名与流式追加并发：改名只写自己那条，不能把追加的消息写回旧快照。"""
    _seed([_sess("tpa")])
    stop = threading.Event()
    appended = [0]

    def appender():
        while not stop.is_set():
            with store.session_write_lock("tpa"):
                s = store.find_session(store.load_sessions(copy=False), "tpa")
                if s is None:
                    break
                s.setdefault("messages", []).append({"role": "assistant", "content": "m"})
                store.save_session(s)
            appended[0] += 1
            time.sleep(0.001)

    t = threading.Thread(target=appender)
    t.start()
    try:
        for i in range(20):
            api._api_session_rename(_H({"id": "tpa", "title": "T%d" % i}))
    finally:
        stop.set()
        t.join()

    store.reset_cache()
    s = store.find_session(store.load_sessions(), "tpa")
    assert s["title"] == "T19"
    assert len(s["messages"]) == appended[0]
