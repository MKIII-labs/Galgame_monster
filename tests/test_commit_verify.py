# -*- coding: utf-8 -*-
"""提交前验证：用符号图找相关测试，只跑它们，失败就拒绝提交。"""
from app import agent


def _mk(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _project(root, body):
    _mk(root, "app/__init__.py", "")
    _mk(root, "app/foo.py", "def add(a, b):\n    return a + b\n")
    _mk(root, "tests/test_foo.py",
        "from app.foo import add\n\n\ndef test_add():\n    " + body + "\n")


def test_verify_passes_and_reports_files(tmp_path):
    _project(tmp_path, "assert add(1, 2) == 3")
    ok, note = agent.verify_before_commit(str(tmp_path), ["app/foo.py"])
    assert ok is True
    assert "已通过" in note
    assert "tests/test_foo.py" in note


def test_verify_blocks_when_test_fails(tmp_path):
    _project(tmp_path, "assert add(1, 2) == 4")
    ok, note = agent.verify_before_commit(str(tmp_path), ["app/foo.py"])
    assert ok is False
    assert "测试失败" in note


def test_verify_skips_when_no_related_test(tmp_path):
    _mk(tmp_path, "lib/bar.py", "VALUE = 1\n")
    ok, note = agent.verify_before_commit(str(tmp_path), ["lib/bar.py"])
    assert ok is True
    assert "跳过验证" in note


def test_commit_requires_message(tmp_path):
    text, _undo = agent._t_git_commit(str(tmp_path), {"message": "  "})
    assert "message" in text
