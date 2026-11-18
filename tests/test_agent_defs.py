# -*- coding: utf-8 -*-
"""子代理定义：内置、frontmatter、只读工具约束、覆盖与删除。"""
import pytest

from app import agent_defs


@pytest.fixture(autouse=True)
def agents_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_defs, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(agent_defs, "USER_AGENTS_DIR", str(tmp_path / "user_agents"))
    return tmp_path / "agents"


def test_builtins_are_readonly():
    ids = {a["id"] for a in agent_defs.list_agents()}
    assert {"researcher", "reviewer", "critic"} <= ids
    for aid in ("researcher", "reviewer", "critic"):
        spec = agent_defs.get_agent(aid)
        assert spec["tools"] and all(t in agent_defs.READ_ONLY_TOOLS for t in spec["tools"])
        assert spec["prompt"]
        assert "write_file" not in spec["tools"]


def test_save_and_override_builtin(agents_dir):
    text = (
        "---\nname: 我的审查员\ndescription: 只读\n"
        "tools: read_file, search_code\nmodel: small-model\nmax_turns: 3\n---\n"
        "只找问题，不改代码。\n")
    spec = agent_defs.save_agent("reviewer", text)
    assert spec["name"] == "我的审查员"
    assert spec["tools"] == ["read_file", "search_code"]
    assert spec["model"] == "small-model" and spec["max_turns"] == 3
    assert (agents_dir / "reviewer.md").is_file()
    got = agent_defs.get_agent("reviewer")
    assert got["prompt"] == "只找问题，不改代码。"


def test_reject_write_tools():
    text = "---\nname: x\ntools: read_file, write_file\n---\n提示词\n"
    with pytest.raises(agent_defs.AgentDefError) as err:
        agent_defs.save_agent("bad", text)
    assert "write_file" in str(err.value)


def test_reject_empty_prompt():
    with pytest.raises(agent_defs.AgentDefError):
        agent_defs.save_agent("bad", "---\nname: x\ntools: read_file\n---\n  ")


def test_delete_override_restores_builtin(agents_dir):
    agent_defs.save_agent("critic", "---\nname: c\ntools: read_file\n---\n自定义\n")
    assert agent_defs.get_agent("critic")["name"] == "c"
    assert agent_defs.delete_agent("critic") is True
    assert agent_defs.get_agent("critic")["name"] == "反证者"
    assert agent_defs.delete_agent("critic") is False


def test_disabled_agent_hidden():
    agent_defs.save_agent(
        "researcher", "---\nname: r\ntools: web_search\nenabled: false\n---\n提示词\n")
    assert agent_defs.get_agent("researcher") is None
    rows = {a["id"]: a for a in agent_defs.list_agents()}
    assert rows["researcher"]["enabled"] is False


def test_role_spec():
    spec = agent_defs.role_spec(agent_defs.get_agent("reviewer"))
    assert spec["prompt"] and spec["tools"]
    assert spec["max_turns"] >= 1

def test_create_rejects_duplicate(agents_dir):
    text = "---\nname: x\ntools: read_file\n---\n提示词\n"
    agent_defs.save_agent("dup", text, create=True)
    with pytest.raises(agent_defs.AgentDefError) as err:
        agent_defs.save_agent("dup", text, create=True)
    assert "已存在" in str(err.value)


def test_rename_removes_old_file(agents_dir):
    agent_defs.save_agent("old", "---\nname: o\ntools: read_file\n---\n提示词\n",
                          create=True)
    agent_defs.save_agent("new", "---\nname: n\ntools: read_file\n---\n提示词\n",
                          old_id="old")
    assert not (agents_dir / "old.md").exists()
    assert (agents_dir / "new.md").is_file()
    assert agent_defs.get_agent("new")["name"] == "n"


def test_rename_rejects_existing_target(agents_dir):
    agent_defs.save_agent("a", "---\nname: a\ntools: read_file\n---\n提示词\n", create=True)
    agent_defs.save_agent("b", "---\nname: b\ntools: read_file\n---\n提示词\n", create=True)
    with pytest.raises(agent_defs.AgentDefError) as err:
        agent_defs.save_agent("b", "---\nname: b\ntools: read_file\n---\n提示词\n",
                              old_id="a")
    assert "已存在" in str(err.value)


def test_set_enabled_builtin_and_restore(agents_dir):
    agent_defs.set_enabled("reviewer", False)
    assert agent_defs.get_agent("reviewer") is None
    rows = {a["id"]: a for a in agent_defs.list_agents()}
    assert rows["reviewer"]["enabled"] is False
    agent_defs.set_enabled("reviewer", True)
    assert agent_defs.get_agent("reviewer")["name"] == "代码审查员"

def test_builtin_descriptions_have_use_and_not_use():
    for aid in agent_defs.BUILTINS:
        desc = agent_defs.get_agent(aid)["description"]
        assert "用于" in desc, aid
        assert "不用于" in desc, aid


def test_scope_priority_project_over_app_over_user(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    user_dir = tmp_path / "user"
    project = tmp_path / "project"
    for d in (app_dir, user_dir, project / ".trae" / "agents"):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_defs, "AGENTS_DIR", str(app_dir))
    monkeypatch.setattr(agent_defs, "USER_AGENTS_DIR", str(user_dir))
    text = "---\nname: %s\ntools: read_file\n---\n提示词\n"
    (app_dir / "reviewer.md").write_text(text % "应用审查", encoding="utf-8")
    (user_dir / "reviewer.md").write_text(text % "用户审查", encoding="utf-8")
    project_file = project / ".trae" / "agents" / "reviewer.md"
    project_file.write_text(text % "项目审查", encoding="utf-8")

    assert agent_defs.get_agent("reviewer", sandbox=str(project))["name"] == "项目审查"
    rows = {a["id"]: a for a in agent_defs.list_agents(sandbox=str(project))}
    assert rows["reviewer"]["name"] == "项目审查"

    project_file.unlink()
    assert agent_defs.get_agent("reviewer", sandbox=str(project))["name"] == "应用审查"
    (app_dir / "reviewer.md").unlink()
    assert agent_defs.get_agent("reviewer", sandbox=str(project))["name"] == "用户审查"
    (user_dir / "reviewer.md").unlink()
    assert agent_defs.get_agent("reviewer", sandbox=str(project))["name"] == "代码审查员"


def test_write_permission_allows_write_tools():
    text = ("---\nname: 实现者\ntools: read_file, write_file, edit_file\n"
            "permission: write\n---\n写代码\n")
    spec = agent_defs.save_agent("impl", text, create=True)
    assert spec["permission"] == "write"
    assert "write_file" in spec["tools"]
    role = agent_defs.role_spec(spec)
    assert role["allow_write"] is True


def test_disallowed_tools_override_tools():
    text = ("---\nname: 只读审查\ntools: read_file, search_code\n"
            "disallowedTools: search_code\n---\n只读\n")
    spec = agent_defs.save_agent("dis", text, create=True)
    assert spec["tools"] == ["read_file", "search_code"]
    assert spec["disallowed_tools"] == ["search_code"]
    assert spec["effective_tools"] == ["read_file"]
    role = agent_defs.role_spec(spec)
    assert role["tools"] == ["read_file"]
    assert role["disallowed_tools"] == ["search_code"]


def test_disallowed_tools_cannot_remove_everything():
    text = "---\nname: x\ntools: read_file\ndisallowedTools: read_file\n---\n提示词\n"
    with pytest.raises(agent_defs.AgentDefError) as err:
        agent_defs.save_agent("empty", text, create=True)
    assert "抵消后为空" in str(err.value)


def test_mcp_servers_roundtrip():
    text = ("---\nname: 外部检索\ntools: read_file\n"
            "mcpServers: GitHub, Filesystem\n---\n提示词\n")
    spec = agent_defs.save_agent("mcp", text, create=True)
    assert spec["mcp_servers"] == ["GitHub", "Filesystem"]
    assert "mcpServers: GitHub, Filesystem" in spec["text"]
    role = agent_defs.role_spec(spec)
    assert role["mcp_servers"] == ["GitHub", "Filesystem"]


def test_write_tools_rejected_without_permission():
    with pytest.raises(agent_defs.AgentDefError) as err:
        agent_defs.save_agent(
            "bad2", "---\nname: x\ntools: read_file, write_file\n---\n提示词\n",
            create=True)
    assert "write_file" in str(err.value)


def test_builtin_implementer_has_write_permission():
    spec = agent_defs.get_agent("implementer")
    assert spec["permission"] == "write"
    assert "write_file" in spec["tools"]
    assert agent_defs.get_agent("planner")["permission"] == "readonly"


def test_general_builtins_exist_and_are_valid():
    ids = {a["id"] for a in agent_defs.list_agents()}
    assert {"tester", "debugger", "security", "documenter", "explainer"} <= ids
    for aid in ("tester", "debugger", "security", "explainer"):
        spec = agent_defs.get_agent(aid)
        assert spec["permission"] == "readonly"
        assert all(t in agent_defs.READ_ONLY_TOOLS for t in spec["tools"])
        assert spec["prompt"].strip()
    doc = agent_defs.get_agent("documenter")
    assert doc["permission"] == "write"
    assert "write_file" in doc["tools"]
