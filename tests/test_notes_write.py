# -*- coding: utf-8 -*-
"""笔记写入：原子写（同目录临时文件 + os.replace）。"""
import pytest

from app import notes


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setattr(notes, "ANNO_DIR", str(tmp_path / "notes" / ".annotations"))
    notes.ensure_notes_dir()
    return tmp_path


def _visible_files(root):
    return sorted(p.name for p in root.iterdir() if not p.name.startswith("."))


def test_write_note_leaves_no_temp_file(sandbox):
    assert notes.write_note("a.md", "# A\n")
    assert notes.read_note("a.md") == "# A\n"
    assert _visible_files(sandbox / "notes") == ["a.md"]


def test_write_note_keeps_old_content_when_replace_fails(sandbox, monkeypatch):
    notes.write_note("a.md", "旧内容")

    def boom(*a, **kw):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(notes.os, "replace", boom)
    assert notes.write_note("a.md", "新内容") is False
    assert (sandbox / "notes" / "a.md").read_text(encoding="utf-8") == "旧内容"  # 旧文件没被截断
    leftovers = [p.name for p in (sandbox / "notes").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []                          # 临时文件已清理


def test_create_note_is_atomic(sandbox):
    ok, rel = notes.create_note("sub/b.md", "# B\n")
    assert ok and rel == "sub/b.md"
    assert notes.read_note("sub/b.md") == "# B\n"
    assert _visible_files(sandbox / "notes" / "sub") == ["b.md"]
