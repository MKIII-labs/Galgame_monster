# -*- coding: utf-8 -*-
"""分享外链图片代理：只回图片、限制大小、失败不裸奔。"""
from app.api import share as share_api


class FakeResponse:
    def __init__(self, status=200, ctype="image/png", chunks=None):
        self.status_code = status
        self.headers = {"Content-Type": ctype} if ctype is not None else {}
        self._chunks = chunks if chunks is not None else [b"\x89PNG", b"rest"]
        self.closed = False

    def iter_content(self, size):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


class FakeHandler:
    def __init__(self, path):
        self.path = path
        self.json = None
        self.sent = None

    def _json(self, obj, code=200):
        self.json = (code, obj)
        return obj

    def _send(self, code, data, ctype, **kw):
        self.sent = (code, data, ctype, kw)
        return data


def test_share_image_returns_bytes(monkeypatch):
    resp = FakeResponse()
    monkeypatch.setattr(share_api, "_public_get", lambda *a, **k: resp)
    h = FakeHandler("/api/share/image?url=https%3A%2F%2Fa.example%2Fx.png")
    share_api._api_share_image(h)
    assert h.sent is not None
    code, data, ctype, _ = h.sent
    assert code == 200 and ctype == "image/png"
    assert data == b"\x89PNGrest"
    assert resp.closed


def test_share_image_rejects_non_image(monkeypatch):
    resp = FakeResponse(ctype="text/html")
    monkeypatch.setattr(share_api, "_public_get", lambda *a, **k: resp)
    h = FakeHandler("/api/share/image?url=https%3A%2F%2Fa.example%2Fx")
    share_api._api_share_image(h)
    assert h.sent is None
    assert h.json[0] == 400


def test_share_image_rejects_oversize(monkeypatch):
    monkeypatch.setattr(share_api, "_MAX_SHARE_IMAGE", 4)
    resp = FakeResponse(chunks=[b"1234", b"5"])
    monkeypatch.setattr(share_api, "_public_get", lambda *a, **k: resp)
    h = FakeHandler("/api/share/image?url=https%3A%2F%2Fa.example%2Fx.png")
    share_api._api_share_image(h)
    assert h.sent is None
    assert h.json[0] == 400


def test_share_image_requires_url(monkeypatch):
    called = []
    monkeypatch.setattr(share_api, "_public_get", lambda *a, **k: called.append(1))
    h = FakeHandler("/api/share/image")
    share_api._api_share_image(h)
    assert not called
    assert h.json[0] == 400
