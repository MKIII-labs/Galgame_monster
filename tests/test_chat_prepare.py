# -*- coding: utf-8 -*-
"""聊天请求入口解析：空请求 / 压缩中 / 会话不存在 / /plan 与附件。"""
from app.api import chat_prepare as cp


class _H:
    """只记录 _json 响应的最小 handler。"""

    def __init__(self):
        self.sent = []

    def _json(self, payload, code=200):
        self.sent.append((payload, code))
        return True


def _reject(h):
    def reject(payload, code=400):
        h._json(payload, code)
    return reject


def _patch(monkeypatch, sessions, busy=False):
    monkeypatch.setattr(cp, "compress_busy", lambda sid: busy)

    def _one(sid, copy=True):
        return next((s for s in (sessions.get("sessions") or [])
                     if s.get("id") == sid), None)

    monkeypatch.setattr(cp, "load_session", _one)


def test_compressing_is_rejected(monkeypatch):
    _patch(monkeypatch, {"sessions": []}, busy=True)
    h = _H()
    req = cp.parse_chat_request({"content": "hi"}, "s1", _reject(h))
    assert req is None
    assert h.sent[-1][1] == 409
    assert h.sent[-1][0]["reason"] == "compressing"


def test_empty_request_is_rejected(monkeypatch):
    _patch(monkeypatch, {"current": "s1", "sessions": [{"id": "s1", "messages": []}]})
    h = _H()
    req = cp.parse_chat_request({}, "s1", _reject(h))
    assert req is None
    assert h.sent[-1] == ({"ok": False, "error": "empty"}, 400)


def test_missing_session_is_rejected(monkeypatch):
    _patch(monkeypatch, {"current": "other", "sessions": []})
    h = _H()
    req = cp.parse_chat_request({"content": "hi"}, "s1", _reject(h))
    assert req is None
    assert h.sent[-1] == ({"ok": False, "error": "no session"}, 404)


def test_plan_prefix_and_image_are_parsed(monkeypatch):
    sess = {"id": "s1", "messages": []}
    _patch(monkeypatch, {"current": "s1", "sessions": [sess]})
    h = _H()
    req = cp.parse_chat_request(
        {"content": "/plan 做点什么",
         "images": [{"data": "abc", "mime": "image/webp"}]},
        "s1", _reject(h))
    assert req is not None
    assert req.plan_mode is True
    assert req.content == "做点什么"
    assert req.has_img is True
    assert req.img_b64 == "abc" and req.img_mime == "image/webp"
    assert req.s is sess
    assert h.sent == []
    # 分段埋点：解析起点 / 仅会话加载耗时（用于修正 load_ms 口径）
    assert req.parse_at > 0
    assert req.load_at >= req.parse_at
    assert req.sess_load_ms >= 0
