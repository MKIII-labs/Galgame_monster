# -*- coding: utf-8 -*-
"""工具元层：核心收敛、action 归一化、子代理白名单与懒加载。"""
import json

from app import agent
from app import tool_meta


def _core():
    return agent.native_tool_schemas(agent_on=True, search_ready=True,
                                     cap_domains=set(), allow_groups=set())


def test_core_tools_are_consolidated():
    schemas = _core()
    names = {s["function"]["name"] for s in schemas}
    assert len(schemas) <= 15
    assert {"read", "edit", "run", "code", "git", "plan", "agent",
            "web", "history"} <= names
    assert "read_file" not in names
    assert "lsp_definition" not in names
    assert len(json.dumps(schemas, ensure_ascii=False)) < 16000


def test_meta_normalize_maps_actions():
    call = agent.normalize_call({"tool": "read",
                                 "args": {"action": "file", "path": "a.py"}})
    assert call == {"tool": "read_file", "args": {"path": "a.py"}}
    call = agent.normalize_call({"tool": "edit",
                                 "args": {"action": "patch", "path": "a.py",
                                          "old": "x", "new": "y"}})
    assert call["tool"] == "edit_file" and call["args"]["old"] == "x"
    call = agent.normalize_call({"tool": "run",
                                 "args": {"action": "terminal", "op": "read",
                                          "command": ""}})
    assert call["tool"] == "shell" and call["args"]["action"] == "read"
    call = agent.normalize_call({"tool": "code",
                                 "args": {"action": "find", "symbol": "Foo"}})
    assert call == {"tool": "find_symbol", "args": {"name": "Foo"}}
    # 没有 action 的旧写法仍走 alias
    assert agent.normalize_call({"tool": "read",
                                 "args": {"path": "a.py"}})["tool"] == "read_file"


def test_meta_call_runs_legacy_handlers(tmp_path):
    (tmp_path / "a.txt").write_text("AAA\n", encoding="utf-8")
    out, _undo, _w = agent.run_tool(str(tmp_path), "read",
                                    {"action": "file", "path": "a.txt"})
    assert "AAA" in out
    out, _undo, is_write = agent.run_tool(
        str(tmp_path), "edit",
        {"action": "patch", "path": "a.txt", "old": "AAA", "new": "BBB"})
    assert is_write is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8").startswith("BBB")
    # 旧名仍直接可用
    out, _undo, _w = agent.run_tool(str(tmp_path), "read_file", {"path": "a.txt"})
    assert "BBB" in out


def test_allowed_tools_limits_actions():
    schemas = agent.native_tool_schemas(
        agent_on=True, search_ready=False, cap_domains=set(),
        allowed_tools={"read_file"})
    assert [s["function"]["name"] for s in schemas] == ["read"]
    enum = schemas[0]["function"]["parameters"]["properties"]["action"]["enum"]
    assert enum == ["file"]


def test_lazy_group_unlocked_builds_meta():
    schemas = agent.native_tool_schemas(
        agent_on=True, search_ready=False, cap_domains=set(),
        allow_groups=set(), unlocked={"asset"})
    asset = next(s for s in schemas if s["function"]["name"] == "asset")
    enum = asset["function"]["parameters"]["properties"]["action"]["enum"]
    assert set(enum) == {"scan", "convert", "atlas", "inline", "validate", "mesh"}


def test_meta_table_has_no_duplicate_actions():
    seen = {}
    for name, meta in tool_meta.META.items():
        for action, item in meta["actions"].items():
            legacy = item[0]
            assert legacy not in seen, "%s 同时被 %s.%s 与 %s 占用" % (
                legacy, name, action, seen.get(legacy))
            seen[legacy] = "%s.%s" % (name, action)


def test_tool_search_reports_meta_actions():
    caps = {}
    text = agent._t_tool_search({"query": "图片"}, caps)
    assert "image" in caps.get("tool_unlock", set())
    assert "see" in text and "compare" in text


def test_legacy_map_is_reverse_of_meta_actions():
    """旧工具名 → 元工具反向表：验收代理的旧名白名单靠它。"""
    m = tool_meta.legacy_map()
    assert m["read_file"] == ("read", "file")
    assert m["run_command"] == ("run", "exec")
    assert m["shell"] == ("run", "terminal")
    assert len(m) == sum(len(spec["actions"]) for spec in tool_meta.META.values())
