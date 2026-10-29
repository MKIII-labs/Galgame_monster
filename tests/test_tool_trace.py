# -*- coding: utf-8 -*-
"""完整工具 trace：追加、按 trace_id 读取、超限滚动。"""
import os

from app import tool_trace


def test_record_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_trace, "TRACE_DIR", str(tmp_path / "trace"))
    tid = tool_trace.record("s1", "read_file", {"path": "a.txt"}, "hello\nworld", True, 12)
    assert tid
    got = tool_trace.load("s1", tid)
    assert got and got["result"] == "hello\nworld"
    assert got["tool"] == "read_file"
    assert got["args"]["path"] == "a.txt"
    assert got["result_chars"] == 11


def test_rotation_keeps_previous_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_trace, "TRACE_DIR", str(tmp_path / "trace"))
    monkeypatch.setattr(tool_trace, "MAX_BYTES", 64)
    first = tool_trace.record("s1", "read_file", {"path": "a"}, "A" * 80, True, 1)
    second = tool_trace.record("s1", "read_file", {"path": "b"}, "B" * 80, True, 2)
    assert first and second
    assert os.path.exists(tool_trace._old_path("s1"))
    assert tool_trace.load("s1", second)["result"] == "B" * 80
    assert tool_trace.load("s1", first)["result"] == "A" * 80


def test_sid_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_trace, "TRACE_DIR", str(tmp_path / "trace"))
    tid = tool_trace.record("../bad/sid", "run_command", {}, "ok", True, 1)
    assert tid
    assert tool_trace.load("../bad/sid", tid)["result"] == "ok"
