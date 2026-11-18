# -*- coding: utf-8 -*-
"""IDE 网页预览：路径收敛（越界 / 密钥 / 不存在）与静态服务行为。"""
import http.server
import socket
import threading
import urllib.error
import urllib.request

import pytest

from app import agent, preview
from app.api import ide_intel


@pytest.fixture(autouse=True)
def _clean_preview():
    preview.stop()
    yield
    preview.stop()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read(), r.headers.get("Cache-Control")
    except urllib.error.HTTPError as e:
        return e.code, b"", e.headers.get("Cache-Control")
    except Exception:          # noqa: BLE001  服务已停：连接被拒
        return "ERR", b"", None


def test_preview_routes_registered():
    from app.registry import snapshot
    routes = snapshot()
    assert ("POST", "/api/ide/preview/start") in routes
    assert ("POST", "/api/ide/preview/stop") in routes


def test_preview_rel_normalizes(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.html").write_text("x", encoding="utf-8")
    assert ide_intel._preview_rel(str(tmp_path), "sub/a.html") == "sub/a.html"
    assert ide_intel._preview_rel(str(tmp_path), "/sub/a.html") == "sub/a.html"
    assert ide_intel._preview_rel(str(tmp_path), ".") == ""


def test_preview_rel_rejects_escape(tmp_path):
    with pytest.raises(agent.SandboxError):
        ide_intel._preview_rel(str(tmp_path), "../outside.html")


def test_preview_rel_rejects_secret(tmp_path):
    (tmp_path / ".env").write_text("KEY=1", encoding="utf-8")
    with pytest.raises(agent.SandboxError, match="密钥"):
        ide_intel._preview_rel(str(tmp_path), ".env")


def test_preview_rel_rejects_missing(tmp_path):
    with pytest.raises(agent.SandboxError, match="找不到文件"):
        ide_intel._preview_rel(str(tmp_path), "nope.html")


def test_preview_server_serves_and_blocks(tmp_path):
    (tmp_path / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=1", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("url=https://t@x", encoding="utf-8")
    base = preview.start(str(tmp_path))
    try:
        status, body, cache = _get(base)
        assert status == 200 and b"<h1>ok</h1>" in body and cache == "no-store"
        assert _get(base + ".env")[0] == 403
        assert _get(base + ".git/config")[0] == 403
        assert _get(base + "nope.html")[0] == 404
    finally:
        preview.stop()
    assert _get(base)[0] not in (200, 403)


def test_preview_injects_bridge_into_html(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body><h1>ok</h1></body></html>", encoding="utf-8")
    (tmp_path / "plain.html").write_text("<h2>plain</h2>", encoding="utf-8")
    (tmp_path / "a.css").write_text("body{}", encoding="utf-8")
    base = preview.start(str(tmp_path))
    try:
        status, body, cache = _get(base)
        assert status == 200 and cache == "no-store"
        assert b"<h1>ok</h1>" in body
        assert b"/__gm_preview_bridge.js" in body
        # 没有 </body> 的片段也能注入
        status, plain, _ = _get(base + "plain.html")
        assert status == 200 and b"<h2>plain</h2>" in plain
        assert b"/__gm_preview_bridge.js" in plain
        # 静态资源不注入
        status, css, _ = _get(base + "a.css")
        assert status == 200 and b"__gm_preview_bridge" not in css
        # 桥脚本本身
        status, js, _ = _get(base + "__gm_preview_bridge.js")
        assert status == 200
        assert b"__gmPreview" in js and b"postMessage" in js
        assert b"console-batch" in js and b"<script" not in js
    finally:
        preview.stop()


def test_inject_html_is_idempotent():
    once = preview._inject_html(b"<html><body>x</body></html>")
    twice = preview._inject_html(once)
    assert once == twice
    assert once.count(b"__gm_preview_bridge.js") == 1


class _DevHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200, extra=None):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send("<html><body><h1>dev</h1></body></html>")
        elif path == "/next":
            self._send("<html><body><h2>next</h2></body></html>")
        elif path == "/asset.js":
            self._send("console.log(1)", "application/javascript")
        elif path == "/redirect":
            self._send("", code=302, extra={"Location": "/next"})
        else:
            self._send("nope", code=404)


def _dev_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DevHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def test_dev_proxy_injects_bridge_and_forwards_assets():
    srv, origin = _dev_server()
    try:
        base = preview.start_dev(origin)
        status, body, cache = _get(base)
        assert status == 200 and b"<h1>dev</h1>" in body
        assert b"__gm_preview_bridge.js" in body and cache == "no-store"
        status, js, _ = _get(base + "asset.js")
        assert status == 200 and js.strip() == b"console.log(1)"
        assert b"__gm_preview_bridge" not in js
        # 无 token 直接访问代理端口：403
        port = int(base.split(":")[2].split("/")[0])
        assert _get("http://127.0.0.1:%d/" % port)[0] == 403
        assert _get(base + "missing")[0] == 404
    finally:
        preview.stop()
        srv.shutdown()
        srv.server_close()


def test_split_target_path():
    assert preview._split_target_path("/") == ("", "/")
    assert preview._split_target_path("/index.html") == ("", "/index.html")
    assert preview._split_target_path("/app/") == ("/app", "/")
    assert preview._split_target_path("/v1.2/") == ("/v1.2", "/")
    assert preview._split_target_path("/app/index.html") == ("/app", "/index.html")


def test_dev_proxy_keeps_target_page_path():
    """带路径的 dev server 地址：代理地址要停在那一页，不能丢掉 /index.html。"""
    srv, origin = _dev_server()
    try:
        base = preview.start_dev(origin + "/index.html")
        assert base.endswith("/index.html")
        status, body, _ = _get(base)
        assert status == 200 and b"<h1>dev</h1>" in body
        # 同一个 dev server 再打开根路径：复用服务，但地址回到 /
        base_root = preview.start_dev(origin + "/")
        assert base_root.endswith("/")
        status, body, _ = _get(base_root)
        assert status == 200 and b"<h1>dev</h1>" in body
    finally:
        preview.stop()
        srv.shutdown()
        srv.server_close()


def test_dev_proxy_file_url_hint():
    with pytest.raises(ValueError, match="path"):
        preview.start_dev("file:///D:/tmp/index.html")


def test_dev_proxy_passes_websocket_upgrade():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _serve():
        # start_dev 会先探一次端口，那是一条空连接；要跳过它再接受真正的 WS 请求
        while True:
            c, _ = srv.accept()
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                pass
            if b"\r\n\r\n" in data:
                break
            c.close()
        try:
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                      b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            c.sendall(c.recv(4096))
        finally:
            c.close()

    threading.Thread(target=_serve, daemon=True).start()
    try:
        base = preview.start_dev("http://127.0.0.1:%d" % port)
        hostpart, token = base.replace("http://", "").strip("/").split("/", 1)
        host, proxy_port = hostpart.split(":")
        s = socket.create_connection((host, int(proxy_port)), timeout=5)
        try:
            s.sendall(("GET /%s/ws HTTP/1.1\r\nHost: %s\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                       "Sec-WebSocket-Version: 13\r\n\r\n"
                       % (token, hostpart)).encode("utf-8"))
            resp = b""
            while b"\r\n\r\n" not in resp:
                resp += s.recv(4096)
            assert b"101 Switching Protocols" in resp
            s.sendall(b"hello")
            assert s.recv(4096) == b"hello"
        finally:
            s.close()
    finally:
        preview.stop()
        srv.close()


def test_dev_proxy_rejects_non_local_and_low_ports():
    with pytest.raises(ValueError):
        preview.start_dev("http://example.com:8080")
    with pytest.raises(ValueError):
        preview.start_dev("http://127.0.0.1:80")
    with pytest.raises(ValueError):
        preview.start_dev("http://127.0.0.1")


class _FakeH:
    def __init__(self, sb, body):
        self.sb = str(sb)
        self.body = body
        self.calls = []

    def _body(self):
        return self.body

    def _sb_guard(self, body):
        return {"id": "s_preview"}, self.sb, None

    def _json(self, obj, code=200):
        self.calls.append((code, obj))
        return obj

    def last(self):
        return self.calls[-1]


def test_preview_start_api_dev_and_static(tmp_path):
    srv, origin = _dev_server()
    try:
        h = _FakeH(tmp_path, {"session_id": "s_preview", "url": origin})
        ide_intel._api_ide_preview_start(h)
        code, r = h.last()
        assert code == 200 and r["ok"] and r["mode"] == "dev"
        assert r["url"].startswith("http://127.0.0.1:")
    finally:
        srv.shutdown()
        srv.server_close()
    (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    h = _FakeH(tmp_path, {"session_id": "s_preview", "path": "index.html"})
    ide_intel._api_ide_preview_start(h)
    code, r = h.last()
    assert code == 200 and r["ok"] and r["mode"] == "static"
    assert r["url"].endswith("/index.html")


def test_open_preview_tool_static_and_dev(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body><h1>tool</h1></body></html>", encoding="utf-8")
    text, undo, is_write = agent.run_tool(
        str(tmp_path), "open_preview", {"path": "index.html"})
    assert "GM_PREVIEW_URL:" in text and is_write is False
    url = text.split("GM_PREVIEW_URL:", 1)[1].split()[0]
    status, body, _ = _get(url)
    assert status == 200 and b"<h1>tool</h1>" in body and b"__gm_preview_bridge.js" in body

    srv, origin = _dev_server()
    try:
        text, _, _ = agent.run_tool(
            str(tmp_path), "open_preview", {"url": origin})
        assert "GM_PREVIEW_URL:" in text
        url = text.split("GM_PREVIEW_URL:", 1)[1].split()[0]
        status, body, _ = _get(url)
        assert status == 200 and b"<h1>dev</h1>" in body
        # 带页面路径的地址：代理必须停在 /index.html，而不是丢掉路径回到根
        text, _, _ = agent.run_tool(
            str(tmp_path), "open_preview", {"url": origin + "/index.html"})
        url = text.split("GM_PREVIEW_URL:", 1)[1].split()[0]
        assert url.endswith("/index.html")
        status, body, _ = _get(url)
        assert status == 200 and b"<h1>dev</h1>" in body
        # file:// 报错必须给出可执行的替代方案（用 path 参数）
        text, _, _ = agent.run_tool(
            str(tmp_path), "open_preview", {"url": "file:///D:/tmp/index.html"})
        assert "path" in text and "http" in text
    finally:
        srv.shutdown()
        srv.server_close()
