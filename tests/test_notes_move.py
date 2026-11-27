# -*- coding: utf-8 -*-
"""笔记：移动笔记 + 目录校验 + 批注随改名/删除迁移。"""
import os

import pytest

from app import anno, db, notes


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "NOTES_DIR", str(tmp_path))
    monkeypatch.setattr(notes, "ANNO_DIR", str(tmp_path / ".annotations"))
    notes.ensure_notes_dir()
    return tmp_path


@pytest.fixture()
def tmpdb(tmp_path):
    old = db.DB_FILE
    db.configure(tmp_path / "test.db")
    yield
    db.configure(old)


def _anno_count(doc_key):
    return len(anno.list_annotations(doc_key))


def test_rename_note_migrates_annotations(sandbox, tmpdb):
    notes.create_note("a.md", "# A")
    anno.save_annotation({"doc_key": "notes:a.md", "quote": "原文", "comment": "批注"})
    ok, new = notes.rename_path("a.md", "b")
    assert ok and new == "b.md"
    assert _anno_count("notes:a.md") == 0
    items = anno.list_annotations("notes:b.md")
    assert len(items) == 1 and items[0]["comment"] == "批注"


def test_move_folder_migrates_annotations(sandbox, tmpdb):
    notes.create_note("proj/a.md", "# A")
    notes.create_folder("archive")
    anno.save_annotation({"doc_key": "notes:proj/a.md", "quote": "x", "comment": "c"})
    ok, new = notes.move_path("proj", "archive")
    assert ok and new == "archive/proj"
    assert _anno_count("notes:proj/a.md") == 0
    assert _anno_count("notes:archive/proj/a.md") == 1


def test_delete_note_drops_annotations(sandbox, tmpdb):
    notes.create_note("a.md", "# A")
    anno.save_annotation({"doc_key": "notes:a.md", "quote": "x", "comment": "c"})
    assert notes.delete_note("a.md") is True
    assert _anno_count("notes:a.md") == 0


def test_delete_folder_drops_only_child_annotations(sandbox, tmpdb):
    notes.create_note("proj/a.md", "# A")
    notes.create_note("keep.md", "# K")
    anno.save_annotation({"doc_key": "notes:proj/a.md", "quote": "x", "comment": "c"})
    anno.save_annotation({"doc_key": "notes:keep.md", "quote": "k", "comment": "k"})
    assert notes.delete_folder("proj") is True
    assert _anno_count("notes:proj/a.md") == 0
    assert _anno_count("notes:keep.md") == 1


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


def test_move_folder_keeps_children(sandbox):
    notes.create_note("proj/a.md", "# A")
    notes.create_folder("archive")
    ok, new = notes.move_path("proj", "archive")
    assert ok and new == "archive/proj"
    assert notes.read_note("archive/proj/a.md") == "# A"
    assert notes.read_note("proj/a.md") is None
    ok, new = notes.move_path("archive/proj", "")
    assert ok and new == "proj"
    assert notes.read_note("proj/a.md") == "# A"


def test_move_folder_rejects_self_and_descendant(sandbox):
    notes.create_folder("a/b")
    ok, err = notes.move_path("a", "a")
    assert not ok and "自身" in err
    ok, err = notes.move_path("a", "a/b")
    assert not ok and "自身" in err


def test_move_folder_rejects_same_name(sandbox):
    notes.create_folder("a/x")
    notes.create_folder("b/x")
    ok, err = notes.move_path("a/x", "b")
    assert not ok and "同名" in err
