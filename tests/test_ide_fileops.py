# -*- coding: utf-8 -*-
"""IDE 文件树右键操作：复制 / 递归删除 / 重命名。"""
import pytest

from app import agent
from app.api import sandbox


class FakeHandler:
    """最小 handler 替身：沙箱固定为 tmp_path。"""

    def __init__(self, sb, body):
        self.sb = str(sb)
        self.body = body
        self.calls = []

    def _body(self):
        return self.body

    def _sb_guard(self, body):
        return {"id": "s_test"}, self.sb, None

    def _json(self, obj, code=200):
        self.calls.append((code, obj))
        return obj

    def last(self):
        return self.calls[-1]


def _copy(tmp_path, body):
    h = FakeHandler(tmp_path, body)
    sandbox._api_sandbox_copy(h)
    return h.last()


def test_copy_file_into_dir(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    code, r = _copy(tmp_path, {"from": "a.txt", "to": "sub"})
    assert code == 200 and r["ok"] and r["path"] == "sub/a.txt"
    assert (tmp_path / "sub" / "a.txt").read_text(encoding="utf-8") == "hello"


def test_copy_same_dir_auto_renames(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    code, r = _copy(tmp_path, {"from": "a.txt", "to": ""})
    assert code == 200 and r["ok"]
    assert r["path"] == "a copy.txt" and r["renamed"] is True
    code, r2 = _copy(tmp_path, {"from": "a.txt", "to": ""})
    assert r2["path"] == "a copy 2.txt"


def test_copy_dir_recursive(tmp_path):
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "a.py").write_text("print(1)", encoding="utf-8")
    (src / "inner").mkdir()
    (src / "inner" / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "dst").mkdir()
    code, r = _copy(tmp_path, {"from": "pkg", "to": "dst"})
    assert code == 200 and r["ok"] and r["path"] == "dst/pkg"
    assert (tmp_path / "dst" / "pkg" / "inner" / "b.txt").read_text(encoding="utf-8") == "b"


def test_copy_dir_into_itself_rejected(tmp_path):
    (tmp_path / "pkg").mkdir()
    code, r = _copy(tmp_path, {"from": "pkg", "to": "pkg"})
    assert code == 400 and not r["ok"]
    code, r = _copy(tmp_path, {"from": "pkg", "to": "pkg/sub"})
    assert code == 400 and not r["ok"]


def test_copy_escape_sandbox_rejected(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    code, r = _copy(tmp_path, {"from": "../a.txt", "to": ""})
    assert code == 400 and not r["ok"]


def test_delete_dir_recursive(tmp_path):
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "a.txt").write_text("x", encoding="utf-8")
    h = FakeHandler(tmp_path, {"path": "pkg", "recursive": True})
    sandbox._api_sandbox_delete(h)
    assert h.last() == (200, {"ok": True})
    assert not d.exists()


def test_delete_dir_non_empty_requires_recursive(tmp_path):
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "a.txt").write_text("x", encoding="utf-8")
    h = FakeHandler(tmp_path, {"path": "pkg"})
    sandbox._api_sandbox_delete(h)
    code, r = h.last()
    assert code == 400 and not r["ok"] and d.exists()


def test_rename_directory(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    (d / "a.txt").write_text("x", encoding="utf-8")
    h = FakeHandler(tmp_path, {"path": "old", "name": "new"})
    sandbox._api_sandbox_rename(h)
    assert h.last() == (200, {"ok": True})
    assert (tmp_path / "new" / "a.txt").exists()
    assert not d.exists()


def test_delete_sandbox_root_rejected(tmp_path):
    with pytest.raises(agent.SandboxError):
        agent.delete_entry(str(tmp_path), ".", recursive=True)
