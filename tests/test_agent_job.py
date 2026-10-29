# -*- coding: utf-8 -*-
"""无头只读巡检：工具白名单、轮数上限、无发现判定，以及与 workflow 的联动"""
import time

import pytest

from app import agent_job, db, workflow


def _conf(monkeypatch):
    monkeypatch.setattr(agent_job, "get_api_conf",
                        lambda provider: ("k", "http://localhost/v1/chat", "m"))


def _fake_stream(monkeypatch, replies):
    """按顺序返回预设响应；用完后一直返回最后一条。返回调用记录。"""
    calls = []

    def fake(api_key, api_url, api_model, messages, **kw):
        calls.append({"messages": [dict(m) for m in messages], "kw": kw})
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(agent_job.llm, "stream_chat", fake)
    return calls


def _reply(visible="", calls=None, usage=None):
    return {"visible": visible, "tool_calls": calls or [], "usage": usage or {}}


def test_readonly_whitelist_blocks_write(monkeypatch, tmp_path):
    _conf(monkeypatch)
    target = tmp_path / "a.txt"
    target.write_text("orig", encoding="utf-8")
    calls = _fake_stream(monkeypatch, [
        _reply(calls=[{"tool": "write_file", "args": {"path": "a.txt", "content": "x"}},
                      {"tool": "read_file", "args": {"path": "a.txt"}}]),
        _reply("发现 a.txt 内容为 orig"),
    ])
    r = agent_job.run("看看 a.txt", str(tmp_path))
    assert r["ok"] and r["findings"] is True
    assert r["tools"] == ["read_file"]
    assert target.read_text(encoding="utf-8") == "orig"
    blob = "\n".join(m["content"] for m in calls[1]["messages"])
    assert "只允许只读工具" in blob
    assert len(calls) == 2


def test_max_rounds_caps_tool_loop(monkeypatch, tmp_path):
    _conf(monkeypatch)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    calls = _fake_stream(monkeypatch, [
        _reply(calls=[{"tool": "read_file", "args": {"path": "a.txt"}}])])
    r = agent_job.run("一直读", str(tmp_path), max_rounds=2)
    assert len(calls) == 2
    assert "最大轮数" in r["text"]
    assert r["findings"] is True          # 没给出结论，不能当无发现静默掉


def test_no_findings_is_quiet(monkeypatch, tmp_path):
    _conf(monkeypatch)
    _fake_stream(monkeypatch, [_reply("NO_FINDINGS")])
    r = agent_job.run("随便看看", str(tmp_path))
    assert r["ok"] and r["findings"] is False
    # 长篇报告里顺带提到 NO_FINDINGS 不算静默
    _fake_stream(monkeypatch, [_reply("NO_FINDINGS 不是结论。\n" + "细节" * 60)])
    assert agent_job.run("随便看看", str(tmp_path))["findings"] is True


def test_rejects_bad_input(monkeypatch, tmp_path):
    _conf(monkeypatch)
    assert agent_job.run("", str(tmp_path))["ok"] is False
    assert agent_job.run("x", "")["ok"] is False
    assert agent_job.run("x", str(tmp_path / "missing"))["ok"] is False


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.configure(tmp_path / "test.db")
    yield


def _wait_run(wid, timeout=4.0):
    end = time.time() + timeout
    runs = []
    while time.time() < end:
        runs = workflow.list_runs(wid)
        if runs and runs[0]["status"] != "running":
            return runs[0]
        time.sleep(0.02)
    return runs[0] if runs else None


def test_workflow_skips_when_no_findings(monkeypatch):
    published = []
    monkeypatch.setattr(workflow.events_bus, "publish", lambda o: published.append(o))
    monkeypatch.setattr(agent_job, "run", lambda *a, **k: {
        "ok": True, "text": "NO_FINDINGS", "findings": False,
        "rounds": 1, "tools": [], "ms": 1})
    wf = workflow.create_workflow("巡检", actions=[
        {"type": "agent.run", "args": {"prompt": "看一下", "sandbox": "x",
                                       "report": "findings"}}])
    workflow.run_workflow(wf["id"])
    run = _wait_run(wf["id"])
    assert run["status"] == "done"
    assert "skipped" in run["detail"]
    assert [o for o in published if o.get("kind") == "automation"] == []


def test_workflow_notifies_when_findings(monkeypatch):
    published = []
    monkeypatch.setattr(workflow.events_bus, "publish", lambda o: published.append(o))
    monkeypatch.setattr(agent_job, "run", lambda *a, **k: {
        "ok": True, "text": "有 2 个测试始终被跳过", "findings": True,
        "rounds": 2, "tools": ["search_files"], "ms": 1})
    wf = workflow.create_workflow("巡检", actions=[
        {"type": "agent.run", "args": {"prompt": "看一下", "sandbox": "x"}}])
    workflow.run_workflow(wf["id"])
    run = _wait_run(wf["id"])
    assert run["status"] == "done"
    assert "有 2 个测试" in run["detail"]
    notes = [o for o in published if o.get("kind") == "automation"]
    assert len(notes) == 1 and notes[0]["type"] == "notify"
