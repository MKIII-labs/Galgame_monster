# -*- coding: utf-8 -*-
"""Hooks：block 语义、上下文注入、fail-open / fail-closed、run_tool 挂点"""
import sys

import pytest

from app import agent, hooks


def _patch(monkeypatch, table):
    monkeypatch.setattr(hooks, "_cfg",
                        lambda: {"hooks_enabled": True, "hooks": table})


def _script(tmp_path, body, name="hook.py"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return '"%s" "%s"' % (sys.executable, str(p))


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(hooks, "_cfg", lambda: {"hooks_enabled": False})
    assert hooks.run("PreToolUse", {"tool": "read_file"})["decision"] == ""


def test_command_block(tmp_path, monkeypatch):
    cmd = _script(tmp_path, 'import json\nprint(json.dumps({"decision": "block", '
                            '"reason": "不行"}))\n')
    _patch(monkeypatch, {"PreToolUse": [{"type": "command", "command": cmd}]})
    res = hooks.before_tool("write_file", {"path": "a"}, str(tmp_path))
    assert res["decision"] == "block"
    assert "不行" in res["reason"]


def test_command_context(tmp_path, monkeypatch):
    cmd = _script(tmp_path, 'import json\nprint(json.dumps({"context": "额外上下文"}))\n')
    _patch(monkeypatch, {"PreToolUse": [{"type": "command", "command": cmd}]})
    assert hooks.before_tool("read_file", {}, str(tmp_path))["contexts"] == ["额外上下文"]


def test_fail_open_by_default(tmp_path, monkeypatch):
    cmd = _script(tmp_path, 'import sys\nsys.exit(3)\n')
    _patch(monkeypatch, {"PreToolUse": [{"type": "command", "command": cmd}]})
    assert hooks.before_tool("read_file", {}, str(tmp_path))["decision"] == ""


def test_fail_closed_when_configured(tmp_path, monkeypatch):
    cmd = _script(tmp_path, 'import sys\nsys.exit(3)\n')
    _patch(monkeypatch, {"PreToolUse": [{"type": "command", "command": cmd,
                                         "fail_open": False}]})
    res = hooks.before_tool("read_file", {}, str(tmp_path))
    assert res["decision"] == "block" and "fail-closed" in res["reason"]


def test_python_handler(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "myhook.py").write_text(
        "def guard(payload):\n"
        "    return {'decision': 'block', 'reason': 'python 拦了'}\n",
        encoding="utf-8")
    monkeypatch.setattr(hooks, "_PLUGIN_DIR", str(plugins))
    _patch(monkeypatch, {"PreToolUse": [{"type": "python", "handler": "myhook:guard"}]})
    res = hooks.before_tool("read_file", {}, str(tmp_path))
    assert res["decision"] == "block" and "python 拦了" in res["reason"]


def test_run_tool_blocked(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    cmd = _script(tmp_path, 'import json\nprint(json.dumps({"decision": "block", '
                            '"reason": "审计拒绝"}))\n')
    _patch(monkeypatch, {"PreToolUse": [{"type": "command", "command": cmd}]})
    text, _undo, _is_write = agent.run_tool(str(tmp_path), "read_file", {"path": "a.txt"})
    assert text.lstrip().startswith("❌")
    assert "审计拒绝" in text


def test_run_tool_context_injected(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    cmd = _script(tmp_path, 'import json\nprint(json.dumps({"context": "记得复查"}))\n')
    _patch(monkeypatch, {"PostToolUse": [{"type": "command", "command": cmd}]})
    text, _undo, _is_write = agent.run_tool(str(tmp_path), "read_file", {"path": "a.txt"})
    assert "hello" in text
    assert "【hook】" in text and "记得复查" in text
