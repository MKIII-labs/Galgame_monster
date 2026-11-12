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


def test_preview_undo_maps_actions_and_relative_paths(tmp_path):
    """预览：改成 modify、AI 新建的删掉、目录新建。"""
    a = tmp_path / "app" / "a.py"
    a.parent.mkdir()
    a.write_text("after", encoding="utf-8")
    b = tmp_path / "notes" / "probe.md"
    b.parent.mkdir()
    b.write_text("x", encoding="utf-8")
    ops = [_op(a, b"before", True), _op(b, b"", False),
           {"kind": "mkdir", "path": str(tmp_path / "docs")}]
    token = agent.register_undo("t-preview", ops, "write_file")
    try:
        data = agent.preview_undo("t-preview", tokens=[token], root=str(tmp_path))
        got = {f["path"]: f["action"] for f in data["files"]}
        assert got == {"app/a.py": "modify", "notes/probe.md": "delete",
                       "docs": "create"}
        assert data["total"] == 1 and data["skipped"] == 0
    finally:
        agent.accept(token)
        _cleanup("t-preview")


def test_preview_undo_last_write_wins(tmp_path):
    """同一文件被写了两次：预览只出现一次，状态以最后一次撤销为准。"""
    p = tmp_path / "c.txt"
    p.write_text("v2", encoding="utf-8")
    t1 = agent.register_undo("t-preview2", [_op(p, b"v0", True)], "write_file")
    t2 = agent.register_undo("t-preview2", [_op(p, b"v1", True)], "edit_file")
    try:
        data = agent.preview_undo("t-preview2", tokens=[t1, t2], root=str(tmp_path))
        assert [(f["path"], f["action"]) for f in data["files"]] == [("c.txt", "modify")]
    finally:
        agent.accept(t1)
        agent.accept(t2)
        _cleanup("t-preview2")


def test_preview_undo_skips_undone_tokens(tmp_path):
    """已经撤销过的步骤不会再动文件，预览里要跳过。"""
    p = tmp_path / "d.txt"
    p.write_text("after", encoding="utf-8")
    token = agent.register_undo("t-preview3", [_op(p, b"before", True)], "edit_file")
    try:
        agent.undo(token)
        data = agent.preview_undo("t-preview3", tokens=[token], root=str(tmp_path))
        assert data["files"] == []
        assert data["skipped"] == 1
    finally:
        agent.accept(token)
        _cleanup("t-preview3")


def test_preview_undo_move_back(tmp_path):
    """move_back：目标会被删、源会被重建。"""
    src = tmp_path / "old.py"
    dst = tmp_path / "new.py"
    dst.write_text("x", encoding="utf-8")
    token = agent.register_undo("t-preview4", [{
        "kind": "move_back", "src": str(src), "dst": str(dst)}], "move_file")
    try:
        data = agent.preview_undo("t-preview4", tokens=[token], root=str(tmp_path))
        got = {f["path"]: f["action"] for f in data["files"]}
        assert got == {"new.py": "delete", "old.py": "create"}
    finally:
        agent.accept(token)
        _cleanup("t-preview4")


def test_ensure_loaded_retries_after_transient_failure(monkeypatch):
    """撤销栈读盘失败时不能把会话标记成「已加载」，否则本进程内永久不再重试。"""
    from app.agent import state as agent_state
    from app.agent.undo import _UNDO
    from app import snapstore

    sid = "t-load-retry"
    token = "tok-retry"
    agent_state._LOADED_SIDS.discard(sid)
    _UNDO.pop(token, None)

    calls = {"n": 0}

    def flaky(_sid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("文件被占用")
        return {token: {"label": "写文件", "kind": "restore", "path": "", "ops": []}}

    monkeypatch.setattr(snapstore, "load", flaky)
    try:
        agent_state._ensure_loaded(sid)       # 第一次失败
        assert calls["n"] == 1
        assert sid not in agent_state._LOADED_SIDS     # 关键：失败不置位
        assert token not in _UNDO

        agent_state._ensure_loaded(sid)       # 第二次会重试并成功
        assert calls["n"] == 2
        assert token in _UNDO
        assert _UNDO[token]["sid"] == sid
        assert sid in agent_state._LOADED_SIDS
    finally:
        _UNDO.pop(token, None)
        agent_state._LOADED_SIDS.discard(sid)
