# -*- coding: utf-8 -*-
"""只读并发预取：全轮收集，写后同路径读与命令后的读保持串行。"""
import threading

from app import agent
from app.api import chat


def _run(monkeypatch, calls, seen=None):
    seen = seen if seen is not None else []

    def fake(sandbox, name, args, allow_outside=False, caps=None, stop_event=None):
        seen.append((name, args.get("path")))
        return "ok", None, False

    monkeypatch.setattr(agent, "run_tool", fake)
    out = chat._prefetch_reads(calls, ".", False, {}, threading.Event())
    return out, seen


def test_same_target():
    assert chat._same_target("a.txt", "a.txt")
    assert chat._same_target("src/a.js", "src")
    assert chat._same_target("src", "src/a.js")
    assert not chat._same_target("src/a.js", "src/b.js")
    assert not chat._same_target("", "a.txt")


def test_reads_after_write_of_other_file_are_prefetched(monkeypatch):
    calls = [
        {"tool": "read_file", "args": {"path": "a.txt"}},
        {"tool": "read_file", "args": {"path": "b.txt"}},
        {"tool": "edit_file", "args": {"path": "a.txt"}},
        {"tool": "read_file", "args": {"path": "a.txt"}},   # 读刚写过的文件：串行
        {"tool": "read_file", "args": {"path": "c.txt"}},   # 不同文件：可以预取
    ]
    out, seen = _run(monkeypatch, calls)
    assert {p for _n, p in seen} == {"a.txt", "b.txt", "c.txt"}
    assert len(out) == 3


def test_reads_after_command_are_not_prefetched(monkeypatch):
    calls = [
        {"tool": "read_file", "args": {"path": "a.txt"}},
        {"tool": "run_command", "args": {"argv": ["echo", "1"]}},
        {"tool": "read_file", "args": {"path": "b.txt"}},
    ]
    out, seen = _run(monkeypatch, calls)
    assert len(seen) == 0          # 少于 2 个任务：不并发，直接返回空
    assert out == {}


def test_search_after_write_is_not_prefetched(monkeypatch):
    calls = [
        {"tool": "read_file", "args": {"path": "a.txt"}},
        {"tool": "read_file", "args": {"path": "b.txt"}},
        {"tool": "write_file", "args": {"path": "x.js"}},
        {"tool": "search_files", "args": {"pattern": "foo"}},
        {"tool": "code_context", "args": {"query": "bar"}},
    ]
    out, seen = _run(monkeypatch, calls)
    assert {p for _n, p in seen} == {"a.txt", "b.txt"}
    assert len(out) == 2
