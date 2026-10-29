# -*- coding: utf-8 -*-
"""外部攻击面加固：同源校验、静态目录包含、SSRF（含 30x 跳转）。"""
from app import httpd, search


# ---------------------------------------------------------------- HTTP 入口
def test_same_origin_rejects_cross_site():
    assert httpd.same_origin_ok("", "127.0.0.1:8000") is True
    assert httpd.same_origin_ok("http://127.0.0.1:8000", "127.0.0.1:8000") is True
    assert httpd.same_origin_ok("http://localhost:8000", "localhost:8000") is True
    # 恶意网页 / 扩展 / null 源一律拒绝
    assert httpd.same_origin_ok("https://evil.example", "127.0.0.1:8000") is False
    assert httpd.same_origin_ok("chrome-extension://abc", "127.0.0.1:8000") is False
    assert httpd.same_origin_ok("null", "127.0.0.1:8000") is False


def test_security_headers_present():
    names = dict(httpd.SECURITY_HEADERS)
    assert names["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors" in names["Content-Security-Policy"]
    assert names["X-Content-Type-Options"] == "nosniff"


def test_static_file_path_blocks_traversal(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "ok.js").write_text("1", encoding="utf-8")
    sibling = tmp_path / "webcraft"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("SECRET", encoding="utf-8")
    monkeypatch.setattr(httpd, "WEB_DIR", str(web))

    assert httpd.static_file_path("ok.js").endswith("ok.js")
    # 旧实现 startswith(WEB_DIR) 会放过 ../webcraft/secret.txt
    assert httpd.static_file_path("../webcraft/secret.txt") == ""
    assert httpd.static_file_path(str(sibling / "secret.txt")) == ""
    assert httpd.static_file_path("../nope.txt") == ""


# ---------------------------------------------------------------- SSRF
def test_public_url_error_blocks_local_and_private(monkeypatch):
    assert search.public_url_error("http://127.0.0.1:8000/x")
    assert search.public_url_error("http://localhost/x")
    assert search.public_url_error("file:///C:/Windows/win.ini")

    monkeypatch.setattr(search.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))])
    assert "内网" in search.public_url_error("http://example.com/")


def test_public_url_error_allows_public(monkeypatch):
    monkeypatch.setattr(search.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert search.public_url_error("https://example.com/page") == ""


def test_public_get_does_not_follow_redirect_to_localhost(monkeypatch):
    """公网页 302 到 127.0.0.1：必须拒绝，且不能发出第二次请求。"""
    def fake_gai(host, *a, **k):
        if host == "public.example":
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        raise OSError("no dns")
    monkeypatch.setattr(search.socket, "getaddrinfo", fake_gai)

    class R:
        def __init__(self, code, headers=None, text=""):
            self.status_code = code
            self.headers = headers or {}
            self.text = text

        def raise_for_status(self):
            return None

    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if url == "https://public.example/":
            return R(302, {"Location": "http://127.0.0.1:8000/secret"})
        return R(200, {}, "SHOULD-NOT-REACH")

    monkeypatch.setattr(search.requests, "get", fake_get)
    assert search._public_get("https://public.example/") is None
    assert calls == ["https://public.example/"]


def test_public_get_returns_plain_response(monkeypatch):
    monkeypatch.setattr(search.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])

    class R:
        status_code = 200
        headers = {}

    monkeypatch.setattr(search.requests, "get", lambda url, **kw: R())
    r = search._public_get("https://example.com/")
    assert r is not None and r.status_code == 200


def test_fetch_text_uses_redirect_guard(monkeypatch):
    """_fetch_one 走 _public_get；到内网的跳转拿不到正文。"""
    import ipaddress

    def fake_gai(host, *a, **k):
        try:                                   # 字面量 IP 按原样返回，才能命中内网判断
            return [(2, 1, 6, "", (str(ipaddress.ip_address(host)), 0))]
        except ValueError:
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(search.socket, "getaddrinfo", fake_gai)
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return None if "r.jina.ai" in url else type(
            "R", (), {"status_code": 302, "headers": {"Location": "http://127.0.0.1:1/"},
                      "text": "", "raise_for_status": lambda self: None})()

    monkeypatch.setattr(search.requests, "get", fake_get)
    assert search.fetch_text("https://example.com/") == ""
    assert not any("127.0.0.1" in u for u in seen)


# ---------------------------------------------------------------- 窗口 / 退出判定
def test_sse_client_counter():
    """SSE 客户端计数用于「窗口还开着就别退出」；不能变负。"""
    base = httpd.active_clients()
    httpd._client_enter()
    assert httpd.active_clients() == base + 1
    httpd._client_leave()
    assert httpd.active_clients() == base
    httpd._client_leave()
    assert httpd.active_clients() == base


def test_window_reconnected_checks_sse_clients(monkeypatch):
    import main
    monkeypatch.setattr(httpd, "active_clients", lambda: 1)
    assert main._window_reconnected(0.3) is True
    monkeypatch.setattr(httpd, "active_clients", lambda: 0)
    assert main._window_reconnected(0.2) is False


def test_kill_stale_tree_never_kills_self(monkeypatch):
    import os

    import main
    calls = []
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: calls.append(a))
    main._kill_stale_tree(os.getpid())
    assert calls == []


def test_kill_stale_tree_kills_other_python(monkeypatch):
    import main
    calls = []
    monkeypatch.setattr(main, "_pid_is_python", lambda pid: True)
    monkeypatch.setattr(main, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: calls.append(a))
    main._kill_stale_tree(999999)
    assert calls and "taskkill" in calls[0][0][0]
    assert "/T" in calls[0][0]
