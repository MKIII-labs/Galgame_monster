# -*- coding: utf-8 -*-
"""笔记：移动笔记 + 目录校验。"""
import os

import pytest

from app import notes


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "NOTES_DIR", str(tmp_path))
    monkeypatch.setattr(notes, "ANNO_DIR", str(tmp_path / ".annotations"))
    notes.ensure_notes_dir()
    return tmp_path


def test_move_note_to_folder(sandbox):
    notes.create_note("a.md", "# A")
    ok, p = notes.create_folder("sub")
    assert ok and p == "sub"
    ok, new = notes.move_note("a.md", "sub")
    assert ok and new == "sub/a.md"
    assert notes.read_note("sub/a.md") == "# A"
    assert notes.read_note("a.md") is None


def test_move_note_to_root(sandbox):
    notes.create_note("a.md", "x")
    notes.create_folder("sub")
    notes.move_note("a.md", "sub")
    ok, new = notes.move_note("sub/a.md", "")
    assert ok and new == "a.md"
    assert os.path.isfile(str(sandbox / "a.md"))


def test_move_note_rejects_conflict_and_bad_dir(sandbox):
    notes.create_note("a.md", "1")
    notes.create_note("sub/a.md", "2")
    ok, err = notes.move_note("a.md", "sub")
    assert not ok and "同名" in err
    ok, err = notes.move_note("a.md", "nope")
    assert not ok and "不存在" in err
    ok, err = notes.move_note("missing.md", "sub")
    assert not ok
