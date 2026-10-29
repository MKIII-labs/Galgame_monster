# -*- coding: utf-8 -*-
"""步级撤销 / 重做 + 撤销栈持久化。"""
from app import agent, snapstore


def _op(path, before, existed=True):
    return {"kind": "restore", "path": str(path), "existed": existed,
            "data": before}


def _cleanup(sid):
    snapstore.clear(sid)


def test_undo_then_redo_roundtrip(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("after", encoding="utf-8")
    token = agent.register_undo("t-roundtrip", [_op(p, b"before")], "edit_file")
    try:
        assert agent.undo(token) is True
        assert p.read_text(encoding="utf-8") == "before"
        assert agent.undo(token) is False        # 同一步不能重复撤销
        assert agent.redo(token) is True
        assert p.read_text(encoding="utf-8") == "after"
        assert agent.redo(token) is False        # 没有撤销过就不能重做
    finally:
        agent.accept(token)
        _cleanup("t-roundtrip")


def test_undo_removes_created_file_and_redo_recreates(tmp_path):
    p = tmp_path / "new.txt"
    p.write_text("x", encoding="utf-8")
    token = agent.register_undo("t-create", [_op(p, b"", existed=False)], "write_file")
    try:
        assert agent.undo(token) is True
        assert not p.exists()
        assert agent.redo(token) is True
        assert p.read_text(encoding="utf-8") == "x"
    finally:
        agent.accept(token)
        _cleanup("t-create")


def test_list_steps_reports_state(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("v", encoding="utf-8")
    token = agent.register_undo("t-list", [_op(p, b"old")], "edit_file")
    try:
        text, _ = agent._t_list_steps(str(tmp_path), {}, {"sid": "t-list"})
        assert token in text and "生效中" in text
        agent.undo(token)
        text2, _ = agent._t_list_steps(str(tmp_path), {}, {"sid": "t-list"})
        assert "已撤销" in text2
    finally:
        agent.accept(token)
        _cleanup("t-list")


def test_snapstore_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(snapstore, "UNDO_DIR", str(tmp_path))
    items = {"a_1": {"label": "edit_file", "ts": 1.0, "undone": True,
                     "ops": [{"kind": "restore", "path": "/x/a.py",
                              "existed": True, "data": b"before",
                              "after": b"after"}]}}
    snapstore.save("sid-x", items)
    got = snapstore.load("sid-x")
    assert got["a_1"]["undone"] is True
    op = got["a_1"]["ops"][0]
    assert op["data"] == b"before"
    assert op["after"] == b"after"


def test_snapstore_skips_oversized_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(snapstore, "UNDO_DIR", str(tmp_path))
    monkeypatch.setattr(snapstore, "MAX_FILE", 4)
    items = {"a_big": {"label": "x", "ts": 1.0, "undone": False,
                       "ops": [{"kind": "restore", "path": "/x",
                                "existed": True, "data": b"12345678"}]}}
    snapstore.save("sid-big", items)
    assert snapstore.load("sid-big") == {}


def test_split_commit_validates_groups(tmp_path):
    text, _ = agent._t_split_commit(str(tmp_path), {"groups": []})
    assert "groups" in text
    text2, _ = agent._t_split_commit(str(tmp_path), {"groups": [
        {"message": "a", "paths": ["x.py"]},
        {"message": "b", "paths": ["x.py"]}]})
    assert "多个分组" in text2
