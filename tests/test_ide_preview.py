# -*- coding: utf-8 -*-
"""IDE 网页预览：路径收敛（越界 / 密钥 / 不存在）与静态服务行为。"""
import urllib.error
import urllib.request

import pytest

from app import agent, preview
from app.api import ide_intel


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
