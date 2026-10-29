# -*- coding: utf-8 -*-
"""变更审阅：接纳（丢弃快照）与批量撤销"""
import pytest

from app import agent
from app.registry import snapshot


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空内存里的撤销 / 本轮检查点，避免互相影响。"""
    with agent._undo_lock:
        agent._UNDO.clear()
    with agent._turns_lock:
        agent._TURNS.clear()
    yield
    with agent._undo_lock:
        agent._UNDO.clear()
    with agent._turns_lock:
        agent._TURNS.clear()


def _register(sid, path, old=b"old"):
    return agent.register_undo(
        sid, [{"kind": "restore", "path": str(path), "existed": True, "data": old}],
        "write_file")


def test_accept_discards_snapshot():
    agent.turn_begin("s1")
    token = _register("s1", "a.txt")
    assert token
    assert agent.undo_old_content(token, "a.txt") == "old"

    assert agent.accept(token) is True
    assert agent.undo_old_content(token, "a.txt") is None
    assert agent.undo(token) is False              # 已接纳，不能再撤回
    assert agent.turn_undo("s1") == (0, 0)         # 本轮检查点里也移除了


def test_accept_unknown_token():
    assert agent.accept("nope") is False
    assert agent.accept_tokens(["nope"]) == 0


def test_undo_tokens_restores_in_reverse(tmp_path):
    """同一文件的多步修改：按相反顺序撤销，最终回到第一次写入前。"""
    p = tmp_path / "f.txt"
    p.write_text("one", encoding="utf-8")
    agent.turn_begin("s2")
    t1 = _register("s2", p, b"one")
    p.write_text("two", encoding="utf-8")
    t2 = _register("s2", p, b"two")
    p.write_text("three", encoding="utf-8")

    assert agent.undo_tokens([t1, t2]) == 2
    assert p.read_text(encoding="utf-8") == "one"


def test_accept_route_registered():
    from app.api import sandbox  # noqa: F401  导入时自注册路由

    assert ("POST", "/api/agent/accept") in snapshot()
