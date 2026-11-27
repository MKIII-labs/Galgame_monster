# -*- coding: utf-8 -*-
"""笔记索引：缓存命中、外部改动刷新、移动/删除后失效。"""
import builtins

import pytest

from app import notes


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setattr(notes, "ANNO_DIR", str(tmp_path / "notes" / ".annotations"))
    monkeypatch.setattr(notes, "LINK_FILE", str(tmp_path / "note_links.json"))
    notes.ensure_notes_dir()
    return tmp_path


@pytest.fixture()
def reads(monkeypatch):
    """统计读盘次数：只记 .md 文件的读（"r"）"""
    real_open = builtins.open
    log = []

    def fake_open(file, mode="r", *a, **kw):
        if "r" in str(mode) and str(file).lower().endswith(".md"):
            log.append(str(file))
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    return log


def test_search_reuses_cache_when_files_unchanged(sandbox, reads):
    notes.write_note("a.md", "# A\n\n梯度下降")
    notes.search("梯度")
    assert len(reads) == 1
    notes.search("梯度")
    assert len(reads) == 1          # 第二次走缓存，不再读盘


def test_external_edit_invalidates_cache(sandbox, reads):
    notes.write_note("a.md", "# A\n\n旧内容")
    assert notes.search("旧内容")
    p = sandbox / "notes" / "a.md"
    p.write_text("# A\n\n全新内容\n", encoding="utf-8")
    assert notes.search("旧内容") == []
    assert notes.search("全新内容")
    assert len(reads) >= 2          # mtime/size 变了，重新读了一次


def test_index_updates_after_write_and_delete(sandbox):
    notes.write_note("a.md", "# A\n\n[[b]]")
    assert [b["path"] for b in notes.backlinks("b.md")] == ["a.md"]
    notes.write_note("a.md", "# A\n\n没有链接了")
    assert notes.backlinks("b.md") == []
    notes.delete_note("a.md")
    assert notes._all_notes() == {}


def test_rename_moves_index_entry(sandbox):
    notes.write_note("旧.md", "# 旧\n关键词")
    ok, new_rel = notes.rename_path("旧.md", "新")
    assert ok and new_rel == "新.md"
    rels = list(notes._all_notes())
    assert rels == ["新.md"]
    assert notes.search("关键词")[0]["path"] == "新.md"


def test_graph_uses_cached_links(sandbox):
    notes.write_note("a.md", "# A\n[[b]] [[不存在]]")
    notes.write_note("b.md", "# B\n[[a]]")
    g = notes.graph()
    edges = {(e["from"], e["to"]) for e in g["edges"]}
    assert ("a.md", "b.md") in edges and ("b.md", "a.md") in edges
    assert any(n["id"] == "missing:不存在" for n in g["nodes"])
    # 缓存内容与 _all_notes 一致
    assert notes._all_notes()["a.md"].startswith("# A")


def test_index_resets_when_root_changes(sandbox, tmp_path, monkeypatch):
    notes.write_note("a.md", "根目录一的内容")
    assert notes.search("根目录一")
    other = tmp_path / "other"
    monkeypatch.setattr(notes, "NOTES_DIR", str(other))
    notes.ensure_notes_dir()
    notes.write_note("a.md", "根目录二的内容")
    assert notes.search("根目录一") == []
    assert notes.search("根目录二")
