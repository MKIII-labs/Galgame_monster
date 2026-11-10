# -*- coding: utf-8 -*-
"""分享产物：脱敏 / 自包含 HTML / .gmshare 包 / 导出管理。"""
import io
import json
import zipfile

import pytest

from app import share
from app.share import package as pkg
from app.share import redact, render


def test_redact_secret_and_path():
    html = '<p>sk-abcdefghijklmnop1234</p><p>C:\\Users\\alice\\secret.txt</p>'
    out, rep = redact.redact_html(html)
    assert "sk-abcdefghijklmnop1234" not in out
    assert "alice" not in out
    assert rep["secrets"] == 1 and rep["paths"] == 1
    assert "<p>" in out                     # 标签结构没被破坏


def test_redact_kv_keeps_key_name():
    out, rep = redact.redact_html('<code>api_key = "abcdef123456"</code>')
    assert "api_key" in out
    assert "abcdef123456" not in out
    assert rep["kv"] == 1


def test_redact_mask_false_only_counts():
    out, rep = redact.redact_html("<p>sk-abcdefghijklmnop1234</p>", mask=False)
    assert "sk-abcdefghijklmnop1234" in out
    assert rep["secrets"] == 1


def test_render_is_self_contained():
    html, sections = render.build_html("t", "q", "<h2>a</h2><p>x</p><h2>b</h2><h2>c</h2>")
    assert "<script" not in html.lower()
    assert "/static/" not in html
    assert "blob:" not in html
    assert "data:font/woff2" in html          # KaTeX 字体已内联
    assert sections == 3 and "sh-toc" in html


def test_render_escapes_question_and_strips_script():
    html, _ = render.build_html("t", "<b>裸标签</b>", '<p>x</p><script>alert(1)</script>')
    assert "&lt;b&gt;" in html
    assert "<script" not in html.lower()


def test_package_roundtrip():
    data = pkg.build({"a": 1}, "<html><body>x</body></html>", title="t")
    item = pkg.read(data)
    assert item["messages"]["a"] == 1
    assert "x" in item["preview"]
    assert item["manifest"]["format"] == "gmshare"


def test_package_rejects_traversal():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "x")
        z.writestr("manifest.json", json.dumps({"format": "gmshare", "version": 1}))
        z.writestr("messages.json", "{}")
        z.writestr("preview.html", "x")
    with pytest.raises(ValueError):
        pkg.read(buf.getvalue())


def test_package_rejects_bad_format():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "other", "version": 1}))
        z.writestr("messages.json", "{}")
        z.writestr("preview.html", "x")
    with pytest.raises(ValueError):
        pkg.read(buf.getvalue())


def test_export_list_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(share, "EXPORT_DIR", str(tmp_path))
    monkeypatch.setattr(share, "IMPORT_DIR", str(tmp_path / "imp"))
    r = share.export({"title": "t", "question": "q", "answer_html": "<p>x</p>"}, "html")
    assert r["ok"] and (tmp_path / r["name"]).exists()
    items = share.list_exports()
    assert items and items[0]["kind"] == "html"
    assert share.delete_export(r["name"]) is True
    assert share.list_exports() == []
