# -*- coding: utf-8 -*-
"""工作台工作流：触发器、动作、占位符、运行历史、事件派发。

（对话里的多阶段编排在 tests/test_orchestrator.py）
"""
import time

import pytest

from app import db, workflow


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    db.configure(tmp_path / "wf.db")
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


def test_create_requires_valid_fields():
    with pytest.raises(ValueError):
        workflow.create_workflow("", actions=[{"type": "notify", "args": {}}])
    with pytest.raises(ValueError):
        workflow.create_workflow("x", trigger_type="nope",
                                 actions=[{"type": "notify", "args": {}}])
    with pytest.raises(ValueError):
        workflow.create_workflow("x", trigger_type="time", trigger_at="",
                                 actions=[{"type": "notify", "args": {}}])
    with pytest.raises(ValueError):
        workflow.create_workflow("x", actions=[])
    with pytest.raises(ValueError):
        workflow.create_workflow("x", actions=[{"type": "unknown"}])


def test_crud_and_toggle():
    wf = workflow.create_workflow("每日提醒", actions=[
        {"type": "notify", "args": {"title": "站会", "text": "10 点"}}])
    assert wf["id"].startswith("w_") and wf["enabled"] is True
    assert len(workflow.list_workflows()) == 1
    assert workflow.get_workflow(wf["id"])["name"] == "每日提醒"

    up = workflow.update_workflow(wf["id"], name="站会提醒",
                                  actions=[{"type": "notify",
                                            "args": {"title": "站会", "text": "10:00"}}])
    assert up["name"] == "站会提醒" and len(up["actions"]) == 1

    off = workflow.set_enabled(wf["id"], False)
    assert off["enabled"] is False

    assert workflow.delete_workflow(wf["id"]) is True
    assert workflow.get_workflow(wf["id"]) is None


def test_next_run_respects_weekdays():
    wf = workflow.create_workflow(
        "工作日", trigger_type="time", trigger_at="08:30",
        trigger_weekdays="0,1,2,3,4",
        actions=[{"type": "notify", "args": {"title": "早", "text": "安"}}])
    assert wf["next_run"] > 0
    assert time.localtime(wf["next_run"]).tm_wday in (0, 1, 2, 3, 4)
    assert time.strftime("%H:%M", time.localtime(wf["next_run"])) == "08:30"

    manual = workflow.create_workflow("手动", actions=[
        {"type": "notify", "args": {"title": "x", "text": "y"}}])
    assert manual["next_run"] == 0


def test_run_executes_actions_in_order(monkeypatch):
    published = []
    monkeypatch.setattr(workflow.events_bus, "publish",
                        lambda o: published.append(o))
    wf = workflow.create_workflow("两步", actions=[
        {"type": "notify", "args": {"title": "第一步", "text": "a"}},
        {"type": "notify", "args": {"title": "第二步", "text": "b"}}])
    rid = workflow.run_workflow(wf["id"])
    assert rid.startswith("r_")
    run = _wait_run(wf["id"])
    assert run["status"] == "done"
    assert [o["title"] for o in published] == ["第一步", "第二步"]
    assert run["detail"].count("notify") == 2
    assert workflow.get_workflow(wf["id"])["last_run"] > 0


def test_run_failure_marks_failed(monkeypatch):
    monkeypatch.setattr(workflow.events_bus, "publish", lambda o: None)
    wf = workflow.create_workflow("坏能力", actions=[
        {"type": "capability", "args": {"id": "no.such.cap", "args": {}}}])
    workflow.run_workflow(wf["id"])
    run = _wait_run(wf["id"])
    assert run["status"] == "failed"
    assert "失败" in run["detail"]


def test_placeholders_are_filled(monkeypatch):
    published = []
    monkeypatch.setattr(workflow.events_bus, "publish",
                        lambda o: published.append(o))
    wf = workflow.create_workflow("占位符", actions=[
        {"type": "notify",
         "args": {"title": "{{date}} {{weekday}}", "text": "现在是 {{time}}"}}])
    workflow.run_workflow(wf["id"])
    _wait_run(wf["id"])
    note = published[0]
    assert "{{" not in note["title"] and "{{" not in note["text"]
    assert "周" in note["title"]
    assert ":" in note["text"]


def test_event_trigger_dispatches(monkeypatch):
    published = []
    monkeypatch.setattr(workflow.events_bus, "publish",
                        lambda o: published.append(o))
    wf = workflow.create_workflow("任务完成提醒", trigger_type="event",
                                  trigger_event="task.done", actions=[
        {"type": "notify",
         "args": {"title": "任务完成", "text": "{{event.title}} 已结束"}}])
    assert wf["trigger_event"] == "task.done"

    assert workflow.emit("unknown.event") == 0
    assert workflow.emit("task.done", {"title": "写报告", "status": "done"}) == 1
    run = _wait_run(wf["id"])
    assert run["status"] == "done"
    assert any("写报告 已结束" in o.get("text", "") for o in published)


def test_event_trigger_skips_disabled(monkeypatch):
    monkeypatch.setattr(workflow.events_bus, "publish", lambda o: None)
    wf = workflow.create_workflow("停用", trigger_type="event",
                                  trigger_event="task.done", enabled=False,
                                  actions=[{"type": "notify",
                                            "args": {"title": "x", "text": "y"}}])
    assert workflow.emit("task.done", {"title": "t"}) == 0
    assert workflow.list_runs(wf["id"]) == []


def test_list_runs_limit_and_scope():
    a = workflow.create_workflow("A", actions=[{"type": "notify",
                                                "args": {"title": "a", "text": "1"}}])
    b = workflow.create_workflow("B", actions=[{"type": "notify",
                                                "args": {"title": "b", "text": "2"}}])
    workflow.run_workflow(a["id"])
    workflow.run_workflow(b["id"])
    _wait_run(a["id"])
    _wait_run(b["id"])
    assert len(workflow.list_runs(a["id"])) == 1
    assert len(workflow.list_runs("")) == 2
    assert len(workflow.list_runs("", 1)) == 1


def test_tick_schedules_due_workflow(monkeypatch):
    monkeypatch.setattr(workflow.events_bus, "publish", lambda o: None)
    wf = workflow.create_workflow("到点", trigger_type="time", trigger_at="08:30",
                                  actions=[{"type": "notify",
                                            "args": {"title": "到点", "text": "x"}}])
    workflow._tick(wf["next_run"] + 1)
    run = _wait_run(wf["id"])
    assert run is not None and run["status"] == "done"
    assert workflow.get_workflow(wf["id"])["next_run"] > 0
