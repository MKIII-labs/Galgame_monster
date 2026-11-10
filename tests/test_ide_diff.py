# -*- coding: utf-8 -*-
"""IDE「查看 AI 改动」依赖的不变量：能按 undo token 取回改动前的原文。"""

from importlib import import_module

import pytest

from app import agent, snapstore

# 包里 re-export 了同名函数 undo()，from app.agent import undo 会拿到函数而不是模块
agent_undo = import_module("app.agent.undo")


@pytest.fixture(autouse=True)
def _undo_dir(tmp_path, monkeypatch):
    """撤销快照落盘改到临时目录：别把测试数据写进真实的 data/undo。"""
    monkeypatch.setattr(snapstore, "UNDO_DIR", str(tmp_path / "undo"))


def _drop(token):
    agent._UNDO.pop(token, None)


def test_undo_old_content_roundtrip(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("new", encoding="utf-8")
    op = {"kind": "restore", "path": str(p), "existed": True, "data": b"old"}
    token = agent.register_undo("s1", [op], "write_file")
    try:
        assert agent.undo_old_content(token, str(p)) == "old"
    finally:
        _drop(token)


def test_undo_old_content_missing_token(tmp_path):
    assert agent.undo_old_content("nope", str(tmp_path / "a.txt")) is None


def test_undo_old_content_other_path(tmp_path):
    p = tmp_path / "a.txt"
    op = {"kind": "restore", "path": str(p), "existed": True, "data": b"old"}
    token = agent.register_undo("s1", [op], "write_file")
    try:
        assert agent.undo_old_content(token, str(tmp_path / "b.txt")) is None
    finally:
        _drop(token)


def test_undo_old_content_created_file(tmp_path):
    """新建文件时 existed=False、data 为空：应返回空串而不是 None。"""
    p = tmp_path / "new.txt"
    op = {"kind": "restore", "path": str(p), "existed": False, "data": b""}
    token = agent.register_undo("s1", [op], "write_file")
    try:
        assert agent.undo_old_content(token, str(p)) == ""
    finally:
        _drop(token)


def test_sandbox_diff_route_registered():
    from app.registry import snapshot
    import app.api.sandbox  # noqa: F401  （导入即注册）

    assert ("POST", "/api/sandbox/diff") in snapshot()


# ============================================================
# 撤销快照的保留策略：本轮不能丢、按会话隔离、重启后能读回来
# ============================================================

def _op(tmp_path, name="a.txt"):
    p = tmp_path / name
    return {"kind": "restore", "path": str(p), "existed": True, "data": b"old"}


def test_current_turn_tokens_are_never_evicted(tmp_path, monkeypatch):
    """一个长回合写 12 次、上限只给 5：本轮 12 条必须全留。

    回归：以前是全局 20 条 FIFO，第 7 个文件的 baseToken 会被后面几十次写入挤掉，
    「查看变更」就报「找不改动前的原始内容」。
    """
    monkeypatch.setattr(agent_undo, "undo_limit", lambda cfg=None: 5)
    sid = "t-keep-turn"
    agent.turn_begin(sid)
    tokens = [agent.register_undo(sid, [_op(tmp_path)], "write_file") for _ in range(12)]
    try:
        assert all(t in agent._UNDO for t in tokens)
        assert agent.undo_old_content(tokens[0], str(tmp_path / "a.txt")) == "old"
    finally:
        for t in tokens:
            _drop(t)


def test_undo_cap_is_per_session(tmp_path, monkeypatch):
    """别的会话写再多次，也不该把我的快照挤掉。"""
    monkeypatch.setattr(agent_undo, "undo_limit", lambda cfg=None: 3)
    a, b = "t-cap-a", "t-cap-b"
    agent.turn_begin(a)
    mine = [agent.register_undo(a, [_op(tmp_path)], "write_file") for _ in range(3)]
    agent.turn_begin(a)                   # a 也换轮：这 3 条变成往期（可被淘汰，但只该淘汰 a 自己的）
    agent.turn_begin(b)
    old_b = [agent.register_undo(b, [_op(tmp_path, "b.txt")], "write_file") for _ in range(6)]
    agent.turn_begin(b)                   # b 换轮：上面 6 条变成「往期」，可被淘汰
    new_b = [agent.register_undo(b, [_op(tmp_path, "b.txt")], "write_file") for _ in range(2)]
    try:
        assert all(t in agent._UNDO for t in mine), "a 的快照被 b 的写入挤掉了"
        left_b = [t for t, v in agent._UNDO.items() if v.get("sid") == b]
        assert len(left_b) == 3           # 只淘汰 b 自己的往期条目
        assert all(t in agent._UNDO for t in new_b)
    finally:
        for t in mine + old_b + new_b:
            _drop(t)


def test_undo_trim_drops_previous_turns(tmp_path, monkeypatch):
    """换成新一轮后，上一轮的快照可以淘汰；本轮的一条都不能少。"""
    monkeypatch.setattr(agent_undo, "undo_limit", lambda cfg=None: 4)
    sid = "t-trim"
    agent.turn_begin(sid)
    old = [agent.register_undo(sid, [_op(tmp_path)], "write_file") for _ in range(4)]
    agent.turn_begin(sid)                 # 新一轮：上一轮不再受保护
    new = [agent.register_undo(sid, [_op(tmp_path)], "write_file") for _ in range(4)]
    try:
        assert all(t in agent._UNDO for t in new)
        assert not any(t in agent._UNDO for t in old)
        assert len([t for t, v in agent._UNDO.items() if v.get("sid") == sid]) == 4
    finally:
        for t in old + new:
            _drop(t)


def test_undo_limit_zero_means_never_trim(tmp_path, monkeypatch):
    """设置里填 0 = 不限：往期快照也不淘汰。"""
    monkeypatch.setattr(agent_undo, "undo_limit", lambda cfg=None: 0)
    sid = "t-unlimited"
    agent.turn_begin(sid)
    first = [agent.register_undo(sid, [_op(tmp_path)], "write_file") for _ in range(3)]
    agent.turn_begin(sid)                 # 全部变成往期
    more = [agent.register_undo(sid, [_op(tmp_path)], "write_file") for _ in range(10)]
    try:
        assert all(t in agent._UNDO for t in first + more)
    finally:
        for t in first + more:
            _drop(t)


def test_ensure_undo_loaded_reads_snapshot_from_disk(tmp_path, monkeypatch):
    """模拟进程重启：内存清空后，看 diff 前要能把磁盘快照读回来。"""
    sid = "t-reload"
    op = _op(tmp_path)
    token = agent.register_undo(sid, [op], "write_file")
    saved = dict(agent._UNDO)
    try:
        agent._UNDO.clear()
        agent._LOADED_SIDS.discard(sid)
        assert agent.undo_old_content(token, str(tmp_path / "a.txt")) is None
        agent.ensure_undo_loaded(sid)
        assert agent.undo_old_content(token, str(tmp_path / "a.txt")) == "old"
    finally:
        agent._UNDO.clear()
        agent._UNDO.update(saved)
        agent._LOADED_SIDS.add(sid)
