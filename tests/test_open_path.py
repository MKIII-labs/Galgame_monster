# -*- coding: utf-8 -*-
"""资源管理器右键菜单：目录 → 会话、投递文件、注册表读写。"""
import os
import sys

import pytest

from app import open_request, open_session, win_context_menu


def _data(*rows):
    return {"current": "", "sessions": list(rows)}


def _session(sid, sandbox, updated):
    return {"id": sid, "title": sid, "sandbox": sandbox, "updated_at": updated,
            "messages": [], "gal_scope": "main"}


def test_norm_dir_accepts_folder_and_file(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    f = folder / "a.txt"
    f.write_text("x", encoding="utf-8")
    assert open_session.norm_dir(str(folder)) == str(folder)
    assert open_session.norm_dir(str(f)) == str(folder)
    assert open_session.norm_dir(str(tmp_path / "nope")) == ""


def test_open_dir_reuses_most_recent(monkeypatch, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    data = _data(_session("old", str(folder), 1),
                 _session("new", str(folder) + os.sep, 2),   # 尾斜杠 / 大小写不该算成另一个目录
                 _session("other", str(tmp_path), 3))
    monkeypatch.setattr(open_session, "load_sessions", lambda: data)
    monkeypatch.setattr(open_session, "save_sessions", lambda d: None)
    monkeypatch.setattr(open_session.saves, "add_save", lambda *a, **k: None)
    s, created = open_session.open_dir_session(str(folder))
    assert created is False
    assert s["id"] == "new"
    assert data["current"] == "new"


def test_open_dir_creates_session(monkeypatch, tmp_path):
    folder = tmp_path / "Game"
    folder.mkdir()
    data = _data()
    monkeypatch.setattr(open_session, "load_sessions", lambda: data)
    monkeypatch.setattr(open_session, "save_sessions", lambda d: None)
    monkeypatch.setattr(open_session.saves, "add_save", lambda *a, **k: None)
    s, created = open_session.open_dir_session(str(folder))
    assert created is True
    assert s["title"] == "Game"
    assert s["sandbox"] == str(folder)
    assert s["agent_enabled"] is True
    assert data["current"] == s["id"]
    assert data["sessions"][0] is s


def test_open_request_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(open_request, "FILE", str(tmp_path / "open.json"))
    assert open_request.pending() is None
    assert open_request.set_pending('"D:\\proj"') is True
    assert (open_request.pending() or {}).get("path") == "D:\\proj"
    open_request.clear()
    assert open_request.pending() is None


def test_open_request_expires(monkeypatch, tmp_path):
    monkeypatch.setattr(open_request, "FILE", str(tmp_path / "open.json"))
    monkeypatch.setattr(open_request, "TTL", -1)          # 任何已写入的请求都算过期
    open_request.set_pending("D:\\proj")
    assert open_request.pending() is None


def test_command_quotes_paths():
    cmd = win_context_menu.command_for(r"C:\py\pythonw.exe",
                                       r"D:\my proj\main.py", "%1")
    assert cmd == '"C:\\py\\pythonw.exe" "D:\\my proj\\main.py" --open "%1"'


def _drop_tree(path):
    """把测试用的注册表子树整个删掉（HKCU 下），别留空壳。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _drop_tree(path + "\\" + sub)
    except OSError:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="仅 Windows 有注册表")
def test_context_menu_registry_roundtrip(tmp_path):
    root = r"Software\GalgameMonsterTest\Classes"
    _drop_tree(r"Software\GalgameMonsterTest")
    main_py = tmp_path / "main.py"
    main_py.write_text("", encoding="utf-8")
    try:
        ok, _msg = win_context_menu.install(python=sys.executable,
                                            main_py=str(main_py), root=root)
        assert ok
        st = win_context_menu.status(root)
        assert st["installed"] is True
        assert "--open" in st["command"] and "%1" in st["command"]
        ok, _msg = win_context_menu.uninstall(root)
        assert ok
        assert win_context_menu.status(root)["installed"] is False
    finally:
        _drop_tree(r"Software\GalgameMonsterTest")
