# -*- coding: utf-8 -*-
"""AUMID 注册：让 Windows 通知显示自己的应用名，而不是 Electron。"""
import sys
import types

from app import win_shortcut as ws


class _Key:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_winreg(store):
    return types.SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_WRITE=2, REG_SZ=1,
        CreateKeyEx=lambda root, path, res, access: _Key(store),
        SetValueEx=lambda key, name, res, typ, val: store.__setitem__(name, val),
    )


def test_identity_writes_display_name_and_icon(monkeypatch, tmp_path):
    icon = tmp_path / "app.ico"
    icon.write_bytes(b"ico")
    store = {}
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(store))
    monkeypatch.setattr(ws.sys, "platform", "win32")
    ok, note = ws.ensure_app_identity("GalgameMonster.Desktop", "Galgame_monster", str(icon))
    assert ok and note.endswith("GalgameMonster.Desktop")
    assert store["DisplayName"] == "Galgame_monster"
    assert store["IconUri"] == str(icon)


def test_identity_skips_non_windows(monkeypatch):
    monkeypatch.setattr(ws.sys, "platform", "linux")
    ok, _note = ws.ensure_app_identity("Any", "x")
    assert ok is False
