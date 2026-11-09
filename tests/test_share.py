# -*- coding: utf-8 -*-
"""分享页：落盘 / 读回 / id 校验 / 接口返回。"""
from app import share
from app.api import share as share_api


class _H:
    """只实现分享路由用到的 HTTP 接口。"""

    def __init__(self, body):
        self._b = body
        self.json = None
        self.code = 0

    def _body(self):
        return self._b

    def _json(self, obj, code=200):
        self.json = obj
        self.code = code
        return obj


def _page(title="分享"):
    return ('<!doctype html><html><head><title>%s</title></head>'
            '<body><p>内容</p></body></html>' % title)


def test_save_and_read_roundtrip():
    item = share.save(_page(), "标题")
    assert item and item["id"].startswith("sh_")
    data = share.read(item["id"])
    assert data and b"<p>" in data
    assert share.read(item["id"] + ".html") == data


def test_reject_non_html_and_oversize():
    assert share.save("") is None
    assert share.save("not html") is None
    assert share.save("<html><body>" + "x" * share.MAX_HTML + "</body></html>") is None


def test_read_rejects_bad_id():
    assert share.read("../etc/passwd") is None
    assert share.read("sh_../x") is None
    assert share.read("missing") is None
    assert share.read("") is None


def test_route_registered():
    from app.registry import snapshot
    import app.api.share  # noqa: F401  导入即注册
    assert ("POST", "/api/share/save") in snapshot()


def test_api_save_returns_url():
    h = _H({"title": "标题", "html": _page()})
    share_api._api_share_save(h)
    assert h.code == 200
    assert h.json["ok"] is True
    assert h.json["url"].startswith("/share/sh_")


def test_api_save_rejects_empty():
    h = _H({"title": "", "html": ""})
    share_api._api_share_save(h)
    assert h.code == 400
    assert h.json["ok"] is False


class _GetStub:
    """只跑 do_GET 的 /share 分支：不碰真实 socket。"""

    def __init__(self, path):
        self.path = path
        self.calls = {}

    def do_GET(self):
        from app.httpd import Handler
        Handler.do_GET(self)

    def _host_ok(self):
        return True

    def _send(self, code, data, mime=""):
        self.calls.update(code=code, data=data, mime=mime)


def test_share_get_serves_html():
    item = share.save(_page(), "标题")
    h = _GetStub("/share/%s.html" % item["id"])
    h.do_GET()
    assert h.calls["code"] == 200
    assert b"<p>" in h.calls["data"]
    assert h.calls["mime"].startswith("text/html")


def test_share_get_missing_is_404():
    h = _GetStub("/share/sh_doesnotexist.html")
    h.do_GET()
    assert h.calls["code"] == 404
