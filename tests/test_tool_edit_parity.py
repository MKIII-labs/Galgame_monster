# -*- coding: utf-8 -*-
"""编辑 / 终端补齐：multi_edit、apply_patch、notebook_edit、shell input/status。"""
import json

from app import agent


def _read(root, rel):
    agent.run_tool(str(root), "read_file", {"path": rel})


def test_multi_edit_applies_multiple_edits_atomically(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("A = 1\nB = 2\nC = 3\n", encoding="utf-8")
    _read(tmp_path, "a.py")
    text, undo, is_write = agent.run_tool(
        str(tmp_path), "multi_edit",
        {"path": "a.py", "edits": [
            {"old": "A = 1", "new": "A = 10"},
            {"old": "C = 3", "new": "C = 30"},
        ]})
    assert is_write is True and undo
    assert "2 处" in text
    assert p.read_text(encoding="utf-8") == "A = 10\nB = 2\nC = 30\n"


def test_multi_edit_failure_leaves_file_untouched(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("A = 1\nA = 1\n", encoding="utf-8")
    _read(tmp_path, "a.py")
    before = p.read_text(encoding="utf-8")
    text, undo, _ = agent.run_tool(
        str(tmp_path), "multi_edit",
        {"path": "a.py", "edits": [
            {"old": "A = 1", "new": "A = 10"},
        ]})
    assert text.startswith("❌") and undo is None
    assert p.read_text(encoding="utf-8") == before


def test_multi_edit_replace_all(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x x x", encoding="utf-8")
    _read(tmp_path, "a.txt")
    text, _, _ = agent.run_tool(
        str(tmp_path), "multi_edit",
        {"path": "a.txt", "edits": [
            {"old": "x", "new": "y", "replace_all": True},
        ]})
    assert "3 处替换" in text
    assert p.read_text(encoding="utf-8") == "y y y"


def test_apply_patch_multiple_hunks(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    _read(tmp_path, "a.py")
    patch = """@@ -1,2 +1,2 @@
 one
-two
+TWO
@@ -4,2 +4,2 @@
 four
-five
+FIVE
"""
    text, undo, is_write = agent.run_tool(
        str(tmp_path), "apply_patch", {"path": "a.py", "patch": patch})
    assert is_write is True and undo
    assert "2 个 hunk" in text
    assert p.read_text(encoding="utf-8") == "one\nTWO\nthree\nfour\nFIVE\n"


def test_apply_patch_context_mismatch_does_not_write(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("one\ntwo\n", encoding="utf-8")
    _read(tmp_path, "a.py")
    before = p.read_text(encoding="utf-8")
    patch = """@@ -1,1 +1,1 @@
 nope
-two
+TWO
"""
    text, undo, _ = agent.run_tool(
        str(tmp_path), "apply_patch", {"path": "a.py", "patch": patch})
    assert text.startswith("❌") and undo is None
    assert p.read_text(encoding="utf-8") == before


def _notebook(tmp_path):
    nb = {
        "cells": [
            {"cell_type": "code", "metadata": {}, "source": "print(1)",
             "execution_count": None, "outputs": []},
            {"cell_type": "markdown", "metadata": {}, "source": "# title"},
        ],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }
    p = tmp_path / "n.ipynb"
    p.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def test_notebook_edit_replace_insert_delete(tmp_path):
    p = _notebook(tmp_path)
    _read(tmp_path, "n.ipynb")

    text, _, _ = agent.run_tool(
        str(tmp_path), "notebook_edit",
        {"path": "n.ipynb", "operation": "replace", "cell_index": 0,
         "source": "print(10)"})
    assert "修改" in text
    nb = json.loads(p.read_text(encoding="utf-8"))
    assert nb["cells"][0]["source"] == "print(10)"

    text, _, _ = agent.run_tool(
        str(tmp_path), "notebook_edit",
        {"path": "n.ipynb", "operation": "insert", "cell_index": 1,
         "source": "print(20)", "cell_type": "code"})
    assert "插入" in text
    nb = json.loads(p.read_text(encoding="utf-8"))
    assert nb["cells"][1]["source"] == "print(20)"

    text, _, _ = agent.run_tool(
        str(tmp_path), "notebook_edit",
        {"path": "n.ipynb", "operation": "delete", "cell_index": 0})
    assert "删除" in text
    nb = json.loads(p.read_text(encoding="utf-8"))
    assert len(nb["cells"]) == 2
    assert nb["cells"][0]["source"] == "print(20)"


def test_shell_input_and_status(tmp_path, monkeypatch):
    from app import terminal

    class FakeTerm:
        def __init__(self):
            self.written = []

        def write(self, text):
            self.written.append(text)
            return True

        def snapshot(self, since=0):
            return {"text": "PS D:\\work> ", "off": 12, "alive": True,
                    "cwd": "D:\\work", "truncated": False, "tid": "t1"}

    fake = FakeTerm()
    monkeypatch.setattr(terminal, "start", lambda *a, **k: fake)
    monkeypatch.setattr(terminal, "get_by_sid", lambda key: fake)

    text, undo, _ = agent.run_tool(
        str(tmp_path), "shell",
        {"action": "input", "text": "y", "enter": True},
        caps={"sid": "s_shell_test"})
    assert "已向终端写入" in text
    assert fake.written == ["y\r"]

    text, undo, _ = agent.run_tool(
        str(tmp_path), "shell", {"action": "status"},
        caps={"sid": "s_shell_test"})
    assert "终端状态：运行中" in text
    assert "工作目录：D:\\work" in text
