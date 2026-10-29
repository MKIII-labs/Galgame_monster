# -*- coding: utf-8 -*-
"""会话置顶：slim 数据带 pinned 字段，置顶排在前面，接口能持久化。"""
import json

import pytest

from app import sessions as store
from app.api import sessions as api_sessions


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SESSION_FILE", str(tmp_path / "sessions.json"))
    store.reset_merge_flag()
    yield tmp_path
    store.reset_merge_flag()


def _write(path, rows):
    path.write_text(json.dumps({"current": rows[0]["id"], "sessions": rows},
                               ensure_ascii=False), encoding="utf-8")


def _sess(sid, pinned=False):
    s = {"id": sid, "title": sid, "updated_at": 1.0, "messages": []}
    if pinned:
        s["pinned"] = True
    return s


class _H:
    def __init__(self, body):
        self.body = body
        self.data = None

    def _body(self):
        return self.body

    def _ok_sessions(self, data):
        self.data = store.slim_sessions(data)


def test_slim_sessions_pinned_first_and_exposes_flag():
    data = {"current": "a", "sessions": [_sess("a"), _sess("b", True), _sess("c")]}
    out = store.slim_sessions(data)
    assert [s["id"] for s in out["sessions"]] == ["b", "a", "c"]
    assert [s["pinned"] for s in out["sessions"]] == [True, False, False]


def test_api_pin_persists(env):
    _write(env / "sessions.json", [_sess("a"), _sess("b")])
    h = _H({"id": "b", "pinned": True})
    api_sessions._api_session_pin(h)

    saved = store.find_session(store.load_sessions(), "b")
    assert saved["pinned"] is True
    # 返回给前端的精简数据也要立刻反映置顶顺序
    assert [s["id"] for s in h.data["sessions"]] == ["b", "a"]


def test_api_unpin(env):
    _write(env / "sessions.json", [_sess("a"), _sess("b", True)])
    h = _H({"id": "b", "pinned": False})
    api_sessions._api_session_pin(h)

    saved = store.find_session(store.load_sessions(), "b")
    assert saved["pinned"] is False
    assert [s["id"] for s in h.data["sessions"]] == ["a", "b"]
