# -*- coding: utf-8 -*-
"""沙箱外只读：绝对路径、批量读、列表展示与相对路径提示。"""
from app import agent


def _paths(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    note = outside / "note.txt"
    note.write_text("OUTSIDE_OK", encoding="utf-8")
    return str(box), str(outside), str(note)


def test_read_file_outside_requires_allow(tmp_path):
    box, _out, note = _paths(tmp_path)
    text, _undo, _write = agent.run_tool(box, "read_file", {"path": note})
    assert "越出沙箱" in text
    text, _undo, _write = agent.run_tool(box, "read_file", {"path": note},
                                         allow_outside=True)
    assert "OUTSIDE_OK" in text


def test_read_files_outside_with_allow(tmp_path):
    box, _out, note = _paths(tmp_path)
    text, _undo, _write = agent.run_tool(box, "read_files", {"paths": [note]},
                                         allow_outside=True)
    assert "OUTSIDE_OK" in text


def test_list_dir_outside_returns_absolute_paths(tmp_path):
    box, out, note = _paths(tmp_path)
    text, _undo, _write = agent.run_tool(box, "list_dir", {"path": out},
                                         allow_outside=True)
    assert "沙箱外" in text
    assert note.replace("\\", "/") in text.replace("\\", "/")
    assert "../" not in text


def test_relative_outside_explains_absolute_requirement(tmp_path):
    box, _out, _note = _paths(tmp_path)
    text, _undo, _write = agent.run_tool(
        box, "read_file", {"path": "../outside/note.txt"}, allow_outside=True)
    assert "绝对路径" in text
