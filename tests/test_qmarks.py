# -*- coding: utf-8 -*-
"""提问标记（qmarks）：负载裁剪与消息撤回后的下标同步。"""
from app import sessions


def _data():
    msgs = [{"role": "user", "content": "q0"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"}]
    return {"current": "s1", "sessions": [{
        "id": "s1", "title": "t", "messages": msgs, "updated_at": 1,
        "qmarks": [{"idx": 0, "label": "first"},
                   {"idx": 2, "label": "second"},
                   {"idx": 99, "label": "已经不存在"}],
    }]}


def test_slim_sessions_keeps_only_valid_qmarks():
    out = sessions.slim_sessions(_data())
    cur = out["sessions"][0]
    assert [m["idx"] for m in cur["qmarks"]] == [0, 2]
    assert cur["qmarks"][0]["label"] == "first"
    assert "at" not in cur["qmarks"][0]      # 前端不需要时间戳


def test_trim_qmarks_drop_after():
    s = {"qmarks": [{"idx": 1, "label": "a"}, {"idx": 3, "label": "b"}]}
    sessions.trim_qmarks(s, drop_after=2)
    assert [m["idx"] for m in s["qmarks"]] == [1]


def test_trim_qmarks_shift_after():
    s = {"qmarks": [{"idx": 1, "label": "a"}, {"idx": 3, "label": "b"}]}
    sessions.trim_qmarks(s, shift_after=1)
    assert [m["idx"] for m in s["qmarks"]] == [1, 2]


def test_trim_qmarks_dedupes_and_skips_bad_entries():
    s = {"qmarks": [{"idx": 1, "label": "a"}, {"idx": 1, "label": "b"},
                    "bad", {"idx": "x"}]}
    sessions.trim_qmarks(s)
    assert [m["idx"] for m in s["qmarks"]] == [1]
    assert s["qmarks"][0]["label"] == "b"     # 后者覆盖前者


class _Handler:
    def __init__(self, body):
        self.body = body
        self.out = None

    def _body(self):
        return self.body

    def _json(self, obj, code=200):
        self.out = (code, obj)
        return self.out

    def _ok_sessions(self, data):
        self.out = (200, {"ok": True, "sessions": sessions.slim_sessions(data)})


def test_qmark_api_add_rename_and_remove(monkeypatch):
    from app.api import sessions as api_sessions
    data = _data()
    data["sessions"][0]["qmarks"] = []
    monkeypatch.setattr(api_sessions, "load_sessions", lambda: data)
    monkeypatch.setattr(api_sessions, "save_sessions", lambda d: None)

    api_sessions._api_session_qmark(_Handler(
        {"id": "s1", "idx": 0, "label": "关键结论", "text": "q0"}))
    assert data["sessions"][0]["qmarks"][0]["label"] == "关键结论"

    # 同一消息再次提交：更新而不是叠加
    api_sessions._api_session_qmark(_Handler(
        {"id": "s1", "idx": 0, "label": "改名后", "text": "q0"}))
    assert len(data["sessions"][0]["qmarks"]) == 1
    assert data["sessions"][0]["qmarks"][0]["label"] == "改名后"

    # 空 label = 删除
    api_sessions._api_session_qmark(_Handler({"id": "s1", "idx": 0, "label": ""}))
    assert data["sessions"][0]["qmarks"] == []


def test_qmark_api_rejects_assistant_message(monkeypatch):
    from app.api import sessions as api_sessions
    data = _data()
    monkeypatch.setattr(api_sessions, "load_sessions", lambda: data)
    monkeypatch.setattr(api_sessions, "save_sessions", lambda d: None)
    h = _Handler({"id": "s1", "idx": 1, "label": "x"})
    api_sessions._api_session_qmark(h)
    assert h.out[0] == 400


def test_qmark_route_registered():
    from app.registry import get
    from app.api import sessions as api_sessions  # noqa: F401  导入即注册
    assert get("POST", "/api/session/qmark") is not None

