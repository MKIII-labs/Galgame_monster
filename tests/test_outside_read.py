# -*- coding: utf-8 -*-
"""沙箱外读取：绝对路径默认可读；写入、秘密文件与 data/ 仍然拒绝。"""
import os

from app import agent
from app.store import DATA_DIR


def _paths(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    note = outside / "note.txt"
    note.write_text("OUTSIDE_OK", encoding="utf-8")
    return str(box), str(outside), str(note)


def test_read_file_outside_by_default(tmp_path):
    box, _out, note = _paths(tmp_path)
    text, _undo, _write = agent.run_tool(box, "read_file", {"path": note})
    assert "OUTSIDE_OK" in text


def test_read_files_outside_by_default(tmp_path):
    box, _out, note = _paths(tmp_path)
    text, _undo, _write = agent.run_tool(box, "read_files", {"paths": [note]})
    assert "OUTSIDE_OK" in text


def test_list_dir_outside_returns_absolute_paths(tmp_path):
    box, out, note = _paths(tmp_path)
    text, _undo, _write = agent.run_tool(box, "list_dir", {"path": out})
    assert "沙箱外" in text
    assert note.replace("\\", "/") in text.replace("\\", "/")
    assert "../" not in text


def test_relative_outside_explains_absolute_requirement(tmp_path):
    box, _out, _note = _paths(tmp_path)
    text, _undo, _write = agent.run_tool(
        box, "read_file", {"path": "../outside/note.txt"})
    assert "绝对路径" in text


def test_write_outside_still_denied(tmp_path):
    box, out, _note = _paths(tmp_path)
    target = os.path.join(out, "new.txt")
    text, _undo, _write = agent.run_tool(
        box, "write_file", {"path": target, "content": "x"})
    assert "越出沙箱" in text
    assert not os.path.exists(target)


def test_secret_file_outside_denied(tmp_path):
    box, out, _note = _paths(tmp_path)
    env = os.path.join(out, ".env")
    with open(env, "w", encoding="utf-8") as f:
        f.write("SECRET=1\n")
    text, _undo, _write = agent.run_tool(box, "read_file", {"path": env})
    assert "默认拒绝读取" in text


def test_data_dir_still_denied(tmp_path):
    box, _out, _note = _paths(tmp_path)
    target = os.path.join(DATA_DIR, "config.json")
    text, _undo, _write = agent.run_tool(box, "read_file", {"path": target})
    assert "data 目录被禁止访问" in text
