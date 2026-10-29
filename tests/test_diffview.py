# -*- coding: utf-8 -*-
"""块级 diff 的不变量：行号、增删计数、块边界与折叠行数。"""

from app import diffview


def _join(lines):
    return "\n".join(lines)


def test_single_hunk_marks_del_and_ins():
    old = _join(["a", "b", "c", "d", "e", "f", "g"])
    new = _join(["a", "b", "c", "X", "d", "e", "f", "g"])
    r = diffview.build(old, new, 1)
    assert r["adds"] == 1 and r["dels"] == 0
    assert len(r["hunks"]) == 1
    h = r["hunks"][0]
    assert h["old"] == ["c", "d"]
    assert h["new"] == ["c", "X", "d"]
    assert h["old_kinds"] == ["ctx", "ctx"]
    assert h["new_kinds"] == ["ctx", "ins", "ctx"]
    assert h["old_start"] == 3 and h["new_start"] == 3
    assert h["gap_before"] == 2


def test_replace_counts_both_sides():
    r = diffview.build(_join(["a", "b", "c"]), _join(["a", "x", "y", "c"]), 0)
    assert r["adds"] == 2 and r["dels"] == 1
    h = r["hunks"][0]
    assert h["old_kinds"] == ["del"]
    assert h["new_kinds"] == ["ins", "ins"]


def test_nearby_changes_merge_into_one_hunk():
    old = _join(["1", "2", "3", "4", "5", "6", "7", "8", "9"])
    new = _join(["1", "X", "3", "4", "5", "6", "Y", "8", "9"])
    r = diffview.build(old, new, 3)
    assert len(r["hunks"]) == 1, "中间只隔 4 行，应并成一块"
    assert r["adds"] == 2 and r["dels"] == 2


def test_far_changes_split_with_gap():
    old = _join([str(i) for i in range(1, 31)])
    new = _join(["X"] + [str(i) for i in range(2, 30)] + ["Y"])
    r = diffview.build(old, new, 2)
    assert len(r["hunks"]) == 2
    assert r["hunks"][0]["gap_before"] == 0
    # 第一块末尾到第二块开头之间被折叠的行数
    first = r["hunks"][0]
    second = r["hunks"][1]
    first_end = first["old_start"] - 1 + len(first["old"])
    assert second["gap_before"] == second["old_start"] - 1 - first_end
    assert second["gap_before"] > 0
    assert r["gap_after"] == 0


def test_pure_insert_new_file():
    r = diffview.build("", _join(["a", "b"]), 3)
    assert r["adds"] == 2 and r["dels"] == 0
    h = r["hunks"][0]
    assert h["old"] == [] and h["old_kinds"] == []
    assert h["new"] == ["a", "b"] and h["new_kinds"] == ["ins", "ins"]


def test_pure_delete():
    r = diffview.build(_join(["a", "b"]), "", 3)
    assert r["adds"] == 0 and r["dels"] == 2
    h = r["hunks"][0]
    assert h["new"] == [] and h["old_kinds"] == ["del", "del"]


def test_no_change_returns_empty_hunks():
    text = _join(["a", "b"])
    r = diffview.build(text, text, 3)
    assert r["hunks"] == [] and r["adds"] == 0 and r["dels"] == 0


def test_crlf_treated_as_lf():
    r = diffview.build("a\r\nb", "a\nc", 0)
    assert r["adds"] == 1 and r["dels"] == 1
    assert r["hunks"][0]["old"] == ["b"]
    assert r["hunks"][0]["new"] == ["c"]
