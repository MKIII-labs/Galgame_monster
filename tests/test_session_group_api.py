# -*- coding: utf-8 -*-
"""分组级操作：置顶 / 重命名（只改显示名）/ 删除组内全部会话。"""
import app.api.sessions as api
from app import sessions


class _H:
    def __init__(self, body):
        self._b = body
        self.code = 200
        self.payload = None

    def _body(self):
        return self._b

    def _json(self, obj, code=200):
        self.code = code
        self.payload = obj
        return obj

    def _ok_sessions(self, data):
        self.payload = {"ok": True, "sessions": data}
        return self.payload


def _cfg(monkeypatch, cfg):
    monkeypatch.setattr(api, "load_config", lambda: cfg)
    monkeypatch.setattr(api, "save_config", lambda c: cfg.update(c))


def test_group_pin(monkeypatch):
    cfg = {"session_group_pins": []}
    _cfg(monkeypatch, cfg)
    h = _H({"op": "pin", "key": r"d:\a", "pinned": True})
    api._api_session_group(h)
    assert h.payload["ok"] is True
    assert cfg["session_group_pins"] == [r"d:\a"]
    h = _H({"op": "pin", "key": r"d:\a", "pinned": False})
    api._api_session_group(h)
    assert cfg["session_group_pins"] == []


def test_group_rename(monkeypatch):
    cfg = {"session_group_names": {}}
    _cfg(monkeypatch, cfg)
    h = _H({"op": "rename", "key": r"d:\a", "name": "项目 A"})
    api._api_session_group(h)
    assert cfg["session_group_names"] == {r"d:\a": "项目 A"}
    # 空名字 = 恢复原名称
    h = _H({"op": "rename", "key": r"d:\a", "name": "  "})
    api._api_session_group(h)
    assert cfg["session_group_names"] == {}


def test_delete_group_removes_sessions_and_meta(monkeypatch):
    key = sessions.sandbox_group(r"D:\A")[0]
    cfg = {"session_group_pins": [key], "session_group_names": {key: "别名"}}
    _cfg(monkeypatch, cfg)
    data = {"current": "s1", "sessions": [
        {"id": "s1", "sandbox": r"D:\A"},
        {"id": "s2", "sandbox": r"D:\A"},
        {"id": "s3", "sandbox": r"D:\B"},
    ]}
    monkeypatch.setattr(api, "load_sessions", lambda: data)
    monkeypatch.setattr(api, "save_sessions", lambda d: None)
    monkeypatch.setattr(api, "delete_session_uploads", lambda sid: None)
    monkeypatch.setattr(api.saves, "drop_saves", lambda sid: None)

    h = _H({"op": "delete", "key": key, "ids": ["s1", "s2"]})
    api._api_session_group(h)

    assert h.payload["ok"] is True
    assert [s["id"] for s in data["sessions"]] == ["s3"]
    assert data["current"] == "s3"
    assert cfg["session_group_pins"] == []
    assert cfg["session_group_names"] == {}


def test_delete_group_rejects_foreign_or_all(monkeypatch):
    key = sessions.sandbox_group(r"D:\A")[0]
    cfg = {}
    _cfg(monkeypatch, cfg)
    data = {"current": "s1", "sessions": [
        {"id": "s1", "sandbox": r"D:\A"},
        {"id": "s2", "sandbox": r"D:\B"},
    ]}
    monkeypatch.setattr(api, "load_sessions", lambda: data)
    monkeypatch.setattr(api, "save_sessions", lambda d: None)

    # 会话不属于该分组
    h = _H({"op": "delete", "key": key, "ids": ["s2"]})
    api._api_session_group(h)
    assert h.payload["ok"] is False
    assert len(data["sessions"]) == 2

    # 不允许把会话删光
    h = _H({"op": "delete", "key": "", "ids": ["s1", "s2"]})
    api._api_session_group(h)
    assert h.payload["ok"] is False
    assert len(data["sessions"]) == 2
