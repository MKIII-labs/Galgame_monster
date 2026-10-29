# -*- coding: utf-8 -*-
"""执行阶段只读并行：并发、预取复用、停止语义。"""
import json
import threading

from app import agent
from app.api import chat


def _key(name, args):
    return (name, json.dumps(args, ensure_ascii=False, sort_keys=True))


def test_run_read_batch_parallelizes(monkeypatch):
    bar = threading.Barrier(2, timeout=2)

    def fake_run_tool(sandbox, name, args, **kw):
        bar.wait()                    # 串行执行会在这里超时
        return "ok-" + args["path"], None, False

    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    calls = [{"tool": "read_file", "args": {"path": "a"}},
             {"tool": "read_file", "args": {"path": "b"}}]
    out = chat._run_read_batch(calls, 0, "sandbox", False, {},
                               threading.Event(), 2)
    assert out[0][0] == "ok-a"
    assert out[1][0] == "ok-b"
    assert out[0][3] >= 0 and out[1][3] >= 0


def test_run_read_batch_reuses_prefetch(monkeypatch):
    called = []

    def fake_run_tool(*a, **kw):
        called.append(a)
        return "x", None, False

    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    calls = [{"tool": "read_file", "args": {"path": "a"}}]
    pf = {_key("read_file", {"path": "a"}): ("cached", None, False, 1)}
    out = chat._run_read_batch(calls, 0, "sandbox", False, {},
                               threading.Event(), 2, pf)
    assert out[0][0] == "cached"
    assert called == []
    assert pf == {}                   # 命中的预取结果已消费


def test_run_read_batch_stop_marks_unexecuted():
    ev = threading.Event()
    ev.set()
    calls = [{"tool": "read_file", "args": {"path": "a"}},
             {"tool": "read_file", "args": {"path": "b"}}]
    out = chat._run_read_batch(calls, 0, "sandbox", False, {}, ev, 2)
    assert all("已停止" in out[i][0] for i in (0, 1))


def test_run_read_batch_keeps_indices(monkeypatch):
    monkeypatch.setattr(agent, "run_tool",
                        lambda sandbox, name, args, **kw: (args["path"], None, False))
    calls = [{"tool": "read_file", "args": {"path": "a"}},
             {"tool": "read_file", "args": {"path": "b"}}]
    out = chat._run_read_batch(calls, 5, "sandbox", False, {},
                               threading.Event(), 2)
    assert out[5][0] == "a" and out[6][0] == "b"
