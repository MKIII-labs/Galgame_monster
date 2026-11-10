# -*- coding: utf-8 -*-
"""工具级权限规则：deny > ask > allow、通配、工具:参数前缀"""
from app import agent
from app.agent import rules as agent_rules


def _rules(**tools):
    return {"allow": [], "ask": [], "deny": [], "commands": {}, "tools": tools}


def _patch(monkeypatch, rules):
    # judge_tool_rule 在函数体内 from .rules import，补丁要打在定义处
    monkeypatch.setattr(agent_rules, "load_effective_rules", lambda sandbox: (rules, ""))


def test_deny_beats_allow(tmp_path, monkeypatch):
    _patch(monkeypatch, _rules(deny=["mcp__*__delete*"], allow=["mcp__fs__*"]))
    d, why = agent.judge_tool_rule(str(tmp_path), "mcp__fs__delete_file", {})
    assert d == "deny" and "delete" in why


def test_allow_glob(tmp_path, monkeypatch):
    _patch(monkeypatch, _rules(allow=["mcp__fs__read_*"]))
    assert agent.judge_tool_rule(str(tmp_path), "mcp__fs__read_file", {})[0] == "allow"
    assert agent.judge_tool_rule(str(tmp_path), "mcp__fs__write_file", {})[0] == ""


def test_arg_prefix_rule(tmp_path, monkeypatch):
    _patch(monkeypatch, _rules(deny=["run_command:git push*"]))
    d, _ = agent.judge_tool_rule(str(tmp_path), "run_command",
                                 {"argv": ["git", "push", "origin", "main"]})
    assert d == "deny"
    # 不是 push 的 git 命令不受这条规则影响
    assert agent.judge_tool_rule(str(tmp_path), "run_command",
                                 {"argv": ["git", "status"]})[0] == ""


def test_ask_rule(tmp_path, monkeypatch):
    _patch(monkeypatch, _rules(ask=["mcp__*"]))
    assert agent.judge_tool_rule(str(tmp_path), "mcp__db__query", {})[0] == "ask"


def test_no_match_returns_empty(tmp_path, monkeypatch):
    _patch(monkeypatch, _rules())
    assert agent.judge_tool_rule(str(tmp_path), "read_file", {"path": "a"}) == ("", "")


def test_parse_tools_section(tmp_path):
    p = tmp_path / ".gm"
    p.mkdir()
    (p / "agent-rules.json").write_text(
        '{"tools":{"deny":["mcp__*__drop"],"allow":["mcp__fs__read_*"]}}',
        encoding="utf-8")
    rules, err = agent.load_rules(str(tmp_path))
    assert err == ""
    assert rules["tools"]["deny"] == ["mcp__*__drop"]
    assert rules["tools"]["allow"] == ["mcp__fs__read_*"]


def test_permissions_alias(tmp_path):
    """`permissions` 是 `tools` 的等价别名，方便照 MCP / Claude Code 的写法抄。"""
    p = tmp_path / ".gm"
    p.mkdir()
    (p / "agent-rules.json").write_text(
        '{"permissions":{"ask":["mcp__*"]}}', encoding="utf-8")
    rules, err = agent.load_rules(str(tmp_path))
    assert err == ""
    assert rules["tools"]["ask"] == ["mcp__*"]
