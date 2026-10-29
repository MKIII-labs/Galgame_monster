# -*- coding: utf-8 -*-
"""IDE 终端：ConPTY 会话（仅 Windows / 装了 pywinpty 时跑）"""
import time

import pytest

from app import terminal

pytestmark = pytest.mark.skipif(
    not terminal.available() or not hasattr(terminal, "Terminal"),
    reason="pywinpty 未安装")


def _read_until(t, off, needle, timeout=12):
    end = time.time() + timeout
    text = ""
    while time.time() < end:
        snap = t.snapshot(off)
        off = snap["off"]
        text += snap["text"]
        if needle in text:
            return text, off
        time.sleep(0.1)
    return text, off


def test_echo_roundtrip(tmp_path):
    t = terminal.start("s_term_echo", str(tmp_path), cols=100, rows=30)
    try:
        _read_until(t, 0, "PS ", 20)          # 等 PowerShell 出第一个提示符
        off = t.snapshot(0)["off"]
        assert t.write("echo gm-terminal-ok\r\n") is True
        text, _ = _read_until(t, off, "gm-terminal-ok")
        assert "gm-terminal-ok" in text
        assert t.snapshot(0)["cwd"]           # 能从提示符里解析出目录
    finally:
        terminal.drop(t.tid)


def test_resize_and_interrupt(tmp_path):
    t = terminal.start("s_term_misc", str(tmp_path), cols=100, rows=30)
    try:
        _read_until(t, 0, "PS ", 20)
        assert t.resize(120, 40) is True
        off = t.snapshot(0)["off"]
        t.write("Start-Sleep -Seconds 30\r\n")
        time.sleep(0.8)
        assert t.interrupt() is True
        _text, _ = _read_until(t, off, "", 3)   # 不要求具体文案，只要求进程还活着
        assert t.alive is True
    finally:
        terminal.drop(t.tid)


def test_kill_marks_dead(tmp_path):
    t = terminal.start("s_term_kill", str(tmp_path), cols=100, rows=30)
    tid = t.tid
    assert terminal.get(tid) is not None
    assert terminal.drop(tid) is True
    assert terminal.get(tid) is None
    assert t.alive is False


def test_buffer_trim(tmp_path):
    t = terminal.start("s_term_buf", str(tmp_path), cols=100, rows=30)
    try:
        _read_until(t, 0, "PS ", 20)
        t._append("x" * (terminal.BUFFER_CHARS + 500))
        snap = t.snapshot(0)
        assert snap["truncated"] is True
        assert len(snap["text"]) <= terminal.BUFFER_CHARS
    finally:
        terminal.drop(t.tid)


# ---------------------------------------------------------------- HTTP 层
class FakeHandler:
    def __init__(self, body):
        self.body = body
        self.out = None
        self.code = 200

    def _body(self):
        return self.body

    def _json(self, obj, code=200):
        self.out = obj
        self.code = code


def test_api_start_requires_sandbox(monkeypatch):
    from app.api import terminal as api_term
    monkeypatch.setattr(api_term, "load_sessions", lambda: {"sessions": []})
    monkeypatch.setattr(api_term, "find_session", lambda data, sid: None)
    h = FakeHandler({"session_id": "s_missing"})
    api_term._api_term_start(h)
    assert h.out["ok"] is False
    assert "会话" in h.out["error"]


def test_api_start_and_input(tmp_path, monkeypatch):
    from app.api import terminal as api_term
    sess = {"sessions": [{"id": "s_api", "sandbox": str(tmp_path)}]}
    monkeypatch.setattr(api_term, "load_sessions", lambda: sess)
    monkeypatch.setattr(api_term, "find_session",
                        lambda data, sid: data["sessions"][0] if sid == "s_api" else None)
    h = FakeHandler({"session_id": "s_api", "cols": 100, "rows": 30})
    api_term._api_term_start(h)
    assert h.out["ok"] is True
    tid = h.out["tid"]
    try:
        t = terminal.get(tid)
        _read_until(t, 0, "PS ", 20)
        h2 = FakeHandler({"tid": tid, "data": "echo api-ok\r\n"})
        api_term._api_term_input(h2)
        assert h2.out["ok"] is True
        off = t.snapshot(0)["off"]
        text, _ = _read_until(t, off, "api-ok")
        assert "api-ok" in text
    finally:
        terminal.drop(tid)
