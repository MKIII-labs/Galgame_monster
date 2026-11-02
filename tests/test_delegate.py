# -*- coding: utf-8 -*-
"""delegate：定义解析、正常派活、未知代理、后台启动。"""
import pytest

from app import agent_defs, delegate, explore


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_defs, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(explore, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(explore, "VERIFY_EVIDENCE", False)


def _fake_chat(ctx, messages, schemas, stop_event, model=None):
    return {"visible": "结论：没有发现问题\n置信度: high；未查清: 无",
            "tool_calls": [], "usage": {"total_tokens": 3}}


def _ctx(tmp_path):
    return {"sandbox": str(tmp_path), "api_key": "k", "api_url": "u",
            "model": "m", "emit": lambda o: None, "budget": 0,
            "chat_fn": _fake_chat, "verify_evidence": False}


def test_run_with_builtin_agent(tmp_path):
    out = delegate.run({"goal": "审一下", "agents": [
        {"agent": "reviewer", "task": "看 app/api"}]}, _ctx(tmp_path))
    assert out["ok"] is True
    assert out["agents"][0]["agent"] == "reviewer"
    assert "reviewer" in out["text"] or "审查" in out["text"]
    assert out["tokens"] == 3


def test_run_with_custom_agent(tmp_path):
    agent_defs.save_agent(
        "ro", "---\nname: 只读员\ntools: read_file\nmax_turns: 2\n---\n只读提示\n",
        create=True)
    out = delegate.run({"agents": [{"agent": "ro", "task": "看 a.py"}]},
                       _ctx(tmp_path))
    assert out["ok"] is True
    assert out["agents"][0]["agent"] == "ro"


def test_unknown_agent_returns_error(tmp_path):
    out = delegate.run({"agents": [{"agent": "nope", "task": "x"}]}, _ctx(tmp_path))
    assert out["ok"] is False
    assert "未知" in out["error"]


def test_background_returns_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(explore, "start_background",
                        lambda args, ctx: {"ok": True, "batch": "ex_test"})
    out = delegate.run({"agents": [{"agent": "reviewer", "task": "x"}],
                        "background": True}, _ctx(tmp_path))
    assert out["ok"] is True and out["batch"] == "ex_test"
    assert "explore_status" in out["text"]


def test_acceptance_passthrough():
    norm, err = explore.normalize_agents(
        {"agents": [{"agent": "reviewer", "task": "x"}],
         "acceptance": ["cites>=1"]})
    assert err == "" and norm["acceptance"] == ["cites>=1"]

def test_pipeline_runs_sequentially(tmp_path):
    seen = []

    def fake_chat(ctx, messages, schemas, stop_event, model=None):
        seen.append(messages[-1].get("content") or "")
        return {"visible": "结论：第%d步完成" % len(seen),
                "tool_calls": [], "usage": {"total_tokens": 2}}

    ctx = _ctx(tmp_path)
    ctx["chat_fn"] = fake_chat
    out = delegate.run({"pipeline": ["reviewer", "critic"], "task": "审一下"},
                       ctx)
    assert out["ok"] is True
    assert "### 1. reviewer" in out["text"] and "### 2. critic" in out["text"]
    assert any("上一步输出" in s for s in seen)


def test_pipeline_rejects_background(tmp_path):
    out = delegate.run({"pipeline": ["reviewer"], "task": "x", "background": True},
                       _ctx(tmp_path))
    assert out["ok"] is False and "后台" in out["error"]

def test_delegate_honors_max_agents(tmp_path):
    agents = [{"agent": "reviewer", "task": "t%d" % i} for i in range(10)]
    _roles, qs, _bad = delegate.resolve(agents, 8)
    assert len(qs) == 8
    ctx = _ctx(tmp_path)
    ctx["max_agents"] = 3
    out = delegate.run({"agents": agents}, ctx)
    assert out["ok"] is True
    assert len(out["agents"]) == 3


def test_parallel_writers_get_isolation_note(tmp_path):
    out = delegate.run({"agents": [
        {"agent": "implementer", "task": "改 a.py"},
        {"agent": "documenter", "task": "改 b.md"}]}, _ctx(tmp_path))
    assert out["ok"] is True
    assert "写隔离" in out["text"] and "implementer" in out["text"]
