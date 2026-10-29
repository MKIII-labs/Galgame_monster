# -*- coding: utf-8 -*-
"""只读工具并发预取：分组规则、去重、异常兜底。

不测真实并发度（线程调度不确定），只测「哪些调用会被预取、结果怎么回填」。
"""
import json
import threading

from app.api import chat


def _call(name, **args):
    return {"tool": name, "args": args}


def _key(name, **args):
    return (name, json.dumps(args, ensure_ascii=False, sort_keys=True))


def test_prefetches_reads_across_round(monkeypatch):
    """全轮预取：写 c.txt 之后，读另一个文件 d.txt 仍然可以并发。"""
    seen = []

    def fake_run(sandbox, name, args, **kw):
        seen.append(name)
        return "内容-%s" % args.get("path"), None, False

    monkeypatch.setattr(chat.agent, "run_tool", fake_run)
    calls = [
        _call("read_file", path="a.txt"),
        _call("read_file", path="b.txt"),
        _call("write_file", path="c.txt", content="x"),
        _call("read_file", path="d.txt"),
    ]
    out = chat._prefetch_reads(calls, ".", False, {}, threading.Event())
    assert set(out) == {_key("read_file", path="a.txt"),
                        _key("read_file", path="b.txt"),
                        _key("read_file", path="d.txt")}
    assert sorted(seen) == ["read_file", "read_file", "read_file"]
    assert out[_key("read_file", path="a.txt")][0] == "内容-a.txt"


def test_read_of_just_written_file_is_not_prefetched(monkeypatch):
    """读刚写过的同一个文件必须等写完成（这里只剩 1 个读，也不值得并发）。"""
    called = []
    monkeypatch.setattr(chat.agent, "run_tool",
                        lambda *a, **k: called.append(1) or ("x", None, False))
    calls = [
        _call("write_file", path="c.txt", content="x"),
        _call("read_file", path="c.txt"),
        _call("read_file", path="d.txt"),
    ]
    out = chat._prefetch_reads(calls, ".", False, {}, threading.Event())
    assert out == {}
    assert not called


def test_single_read_is_not_prefetched():
    calls = [_call("read_file", path="a.txt")]
    assert chat._prefetch_reads(calls, ".", False, {}, threading.Event()) == {}


def test_duplicate_reads_run_once(monkeypatch):
    seen = []

    def fake_run(sandbox, name, args, **kw):
        seen.append(args.get("path"))
        return "x", None, False

    monkeypatch.setattr(chat.agent, "run_tool", fake_run)
    calls = [
        _call("read_file", path="a.txt"),
        _call("read_file", path="a.txt"),
        _call("read_file", path="b.txt"),
    ]
    out = chat._prefetch_reads(calls, ".", False, {}, threading.Event())
    assert sorted(seen) == ["a.txt", "b.txt"]
    assert len(out) == 2


def test_tool_exception_becomes_error_text(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(chat.agent, "run_tool", boom)
    calls = [_call("read_file", path="a.txt"), _call("read_file", path="b.txt")]
    out = chat._prefetch_reads(calls, ".", False, {}, threading.Event())
    assert all(v[0].startswith("❌") for v in out.values())


def test_stopped_before_run(monkeypatch):
    """用户已点停止 → 预取不再执行任何工具"""
    called = []
    monkeypatch.setattr(chat.agent, "run_tool",
                        lambda *a, **k: called.append(1) or ("x", None, False))
    ev = threading.Event()
    ev.set()
    calls = [_call("read_file", path="a.txt"), _call("read_file", path="b.txt")]
    out = chat._prefetch_reads(calls, ".", False, {}, ev)
    assert not called
    assert all(v[0].startswith("❌") for v in out.values())
