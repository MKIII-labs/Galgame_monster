# -*- coding: utf-8 -*-
"""回退预览：磁盘冲突标记 + 跳过判断；影子快照仓库的检查点。"""
import os
from importlib import import_module

import pytest

from app.agent import read_tracking, snapshot

# 包里 re-export 了同名函数 undo()，from app.agent import undo 会拿到函数而不是模块
U = import_module("app.agent.undo")
STATE = import_module("app.agent.state")


@pytest.fixture
def env(tmp_path, monkeypatch):
    sid = "s_undo_preview"
    read_tracking.turn_begin(sid)
    monkeypatch.setattr(U, "ensure_undo_loaded", lambda _sid: None)
    monkeypatch.setattr(U, "_persist_undo", lambda _sid: None)
    monkeypatch.setattr(snapshot, "SNAP_DIR", str(tmp_path / "snaps"))
    yield sid, tmp_path
    U._UNDO.clear()
    STATE._TURNS.pop(sid, None)


def _write(path, text):
    path.write_bytes(text.encode("utf-8"))    # 避免 Windows 换行翻译影响字节比对


def _register(sid, path, root=""):
    op = U._snap(str(path))
    token = U.register_undo(sid, [op], "测试写入", root=root)
    return token


def test_preview_marks_conflict_and_skip(env):
    sid, tmp = env
    p = tmp / "a.txt"
    _write(p, "old\n")
    op = U._snap(str(p))
    _write(p, "new\n")
    token = U.register_undo(sid, [op], "写 a")
    assert token

    # 用户手改成第三版 → 冲突
    _write(p, "user edit\n")
    pv = U.preview_undo(sid, tokens=[token], root=str(tmp))
    row = next(f for f in pv["files"] if f["path"] == "a.txt")
    assert row["action"] == "modify"
    assert row["conflict"] is True and "手动改过" in row["reason"]
    assert pv["conflicts"] == 1

    # 改回 AI 写完的版本 → 不再冲突
    _write(p, "new\n")
    pv = U.preview_undo(sid, tokens=[token], root=str(tmp))
    row = next(f for f in pv["files"] if f["path"] == "a.txt")
    assert row["conflict"] is False

    # 已经是目标内容 → 跳过
    _write(p, "old\n")
    pv = U.preview_undo(sid, tokens=[token], root=str(tmp))
    row = next(f for f in pv["files"] if f["path"] == "a.txt")
    assert row["skipped"] is True and row["reason"]


def test_preview_created_and_deleted_files(env):
    sid, tmp = env
    created = tmp / "new.txt"
    token = _register(sid, created, root=str(tmp))
    _write(created, "hello\n")
    # 注册时 after 还没写：补一次 after 快照
    U._capture_after(U._UNDO[token]["ops"])

    pv = U.preview_undo(sid, tokens=[token], root=str(tmp))
    row = next(f for f in pv["files"] if f["path"] == "new.txt")
    assert row["action"] == "delete"

    created.unlink()
    pv = U.preview_undo(sid, tokens=[token], root=str(tmp))
    row = next(f for f in pv["files"] if f["path"] == "new.txt")
    assert row["skipped"] is True


def test_undo_skips_when_already_at_target(env):
    sid, tmp = env
    p = tmp / "b.txt"
    _write(p, "before\n")
    op = U._snap(str(p))
    _write(p, "after\n")
    token = U.register_undo(sid, [op], "写 b")

    _write(p, "before\n")          # 用户手动改回去了
    assert U.undo(token) is True
    assert p.read_text(encoding="utf-8") == "before\n"


@pytest.mark.skipif(not snapshot.available(), reason="本机没有 git，跳过影子仓库测试")
def test_shadow_repo_checkpoint_and_read(env):
    sid, tmp = env
    root = tmp / "ws"
    root.mkdir()
    p = root / "c.txt"
    _write(p, "v1\n")
    op = U._snap(str(p))
    _write(p, "v2\n")
    token = U.register_undo(sid, [op], "写 c", root=str(root))
    assert token

    tags = snapshot.list_tags(str(root))
    assert any(t.startswith("before-chat-turn-") for t in tags)
    tool_tags = [t for t in tags if t.startswith("toolcall-")]
    assert tool_tags
    assert snapshot.read_file(str(root), tool_tags[-1], "c.txt") == b"v1\n"
    assert snapshot.read_file(str(root), tool_tags[-1], "c.txt", which="content") == b"v2\n"
