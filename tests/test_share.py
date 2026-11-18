# -*- coding: utf-8 -*-
"""分享产物：脱敏 / 自包含 HTML / .gmshare 包 / 导出管理。"""
import io
import json
import os
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


def test_default_export_dir_is_outside_project():
    """导出目录必须在「下载」里，不能落在项目 data/ 下。"""
    from app.store import DATA_DIR
    assert os.path.basename(share.DEFAULT_EXPORT_DIR) == "Galgame Monster 分享"
    assert not os.path.abspath(share.DEFAULT_EXPORT_DIR).startswith(
        os.path.abspath(DATA_DIR) + os.sep)


def test_legacy_exports_migrate_to_new_dir(tmp_path, monkeypatch):
    """老版本留在 data/shares/exports 的文件应搬到新导出目录。"""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "sh_0123456789ab.html").write_text("<html><body>x</body></html>",
                                                 encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(share, "EXPORT_DIR", str(dest))
    monkeypatch.setattr(share, "DEFAULT_EXPORT_DIR", str(dest))
    monkeypatch.setattr(share, "_LEGACY_EXPORT_DIR", str(legacy))
    share._migrate_legacy_exports()
    assert (dest / "sh_0123456789ab.html").is_file()
    assert not (legacy / "sh_0123456789ab.html").exists()


def test_export_path_rejects_bad_names(tmp_path, monkeypatch):
    monkeypatch.setattr(share, "EXPORT_DIR", str(tmp_path))
    (tmp_path / "sh_0123456789ab.html").write_text("x", encoding="utf-8")
    assert share.export_path("sh_0123456789ab.html")
    assert share.export_path("../config.json") == ""
    assert share.export_path("sh_ffffffffffff.exe") == ""
    assert share.export_path("sh_deadbeefdead.exe") == ""    # 不存在


def test_export_copies_to_chosen_path(tmp_path, monkeypatch):
    """「另存为」选择的位置要收到一份副本，导出记录仍留在导出目录。"""
    monkeypatch.setattr(share, "EXPORT_DIR", str(tmp_path / "out"))
    target = tmp_path / "desktop" / "我的分享.html"
    r = share.export({"title": "t", "question": "q", "answer_html": "<p>x</p>"},
                     "html", save_path=str(target))
    assert r["ok"] and r["saved_to"] == str(target)
    assert target.is_file()
    assert r["path"] != str(target) and os.path.isfile(r["path"])

    # 没写扩展名时自动补全
    r2 = share.export({"title": "t2", "question": "q", "answer_html": "<p>y</p>"},
                      "html", save_path=str(tmp_path / "noext"))
    assert r2["saved_to"].endswith(".html") and os.path.isfile(r2["saved_to"])


def test_ask_save_path_falls_back_and_cancel(monkeypatch):
    class _P:
        returncode = 0
        stdout = "\n__GM_PICK__\n"        # 子进程正常退出、用户取消

    monkeypatch.setattr(share.subprocess, "run", lambda *a, **k: _P())
    assert share.ask_save_path("x.html", "html") == ""

    class _Junk:
        returncode = 0
        stdout = "some vendor log\n__GM_PICK__D:/tmp/x.html\n"

    monkeypatch.setattr(share.subprocess, "run", lambda *a, **k: _Junk())
    assert share.ask_save_path("x.html", "html") == os.path.abspath("D:/tmp/x.html")

    def _boom(*a, **k):
        raise OSError("no tkinter")

    monkeypatch.setattr(share.subprocess, "run", _boom)
    assert share.ask_save_path("x.html", "html") is None     # 窗口打不开


class _FakeHandler:
    def __init__(self, body):
        self._body_obj = body
        self.out = None

    def _body(self):
        return self._body_obj

    def _json(self, obj, code=200):
        self.out = obj
        return obj


def test_api_export_cancel_writes_nothing(tmp_path, monkeypatch):
    from app.api import share as api_share
    monkeypatch.setattr(share, "EXPORT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(share, "ask_save_path", lambda *a, **k: "")
    h = _FakeHandler({"format": "html", "save_as": True, "title": "t",
                      "answer_html": "<p>x</p>"})
    out = api_share._api_share_export(h)
    assert out["canceled"] is True and not out["ok"]
    assert not (tmp_path / "out").exists()


def test_api_export_save_as_uses_picked_path(tmp_path, monkeypatch):
    from app.api import share as api_share
    monkeypatch.setattr(share, "EXPORT_DIR", str(tmp_path / "out"))
    picked = str(tmp_path / "桌面" / "我的分享.html")
    monkeypatch.setattr(share, "ask_save_path", lambda *a, **k: picked)
    h = _FakeHandler({"format": "html", "save_as": True, "title": "t",
                      "answer_html": "<p>x</p>"})
    out = api_share._api_share_export(h)
    assert out["ok"] and out["saved_to"] == picked
    assert os.path.isfile(picked)
