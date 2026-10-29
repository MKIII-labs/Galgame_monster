# -*- coding: utf-8 -*-
"""任务引擎：状态机、手动步骤、能力执行、写操作确认、重启收敛"""
import time

import pytest

from app import calendar, db, task_engine


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.configure(tmp_path / "test.db")
    yield


def wait_status(tid, status, timeout=5.0):
    end = time.time() + timeout
    task = None
    while time.time() < end:
        task = task_engine.get_task(tid)
        if task and task["status"] == status:
            return task
        time.sleep(0.02)
    return task


def test_manual_steps_complete_one_by_one():
    t = task_engine.create_task("写周报", ["收集材料", "写初稿", "发出"])
    task_engine.start_task(t["id"])
    t = wait_status(t["id"], "paused")
    assert t["status"] == "paused"
    assert t["steps"][0]["status"] == "pending"      # 没有工具 → 等用户勾

    for step in t["steps"]:
        task_engine.complete_step(t["id"], step["id"])
    t = wait_status(t["id"], "done")
    assert t["status"] == "done"
    assert t["progress"]["percent"] == 100


def test_read_capability_runs_automatically():
    t = task_engine.create_task("查日程", [
        {"title": "查询", "tool": "calendar.list", "args": {}},
    ])
    task_engine.start_task(t["id"])
    t = wait_status(t["id"], "done")
    assert t["status"] == "done"
    assert t["steps"][0]["status"] == "done"


def test_write_step_waits_for_confirmation():
    t = task_engine.create_task("建日程", [
        {"title": "创建", "tool": "calendar.create",
         "args": {"title": "引擎测试", "start_at": "2026-12-01 09:00"}},
    ])
    task_engine.start_task(t["id"])
    t = wait_status(t["id"], "paused")
    assert t["steps"][0]["status"] == "blocked"

    task_engine.confirm_step(t["id"], t["steps"][0]["id"])
    t = wait_status(t["id"], "done")
    assert t["status"] == "done"
    assert calendar.list_events(calendar.parse_time("2026-12-01 00:00"),
                                calendar.parse_time("2026-12-02 00:00"))


def test_cancel():
    t = task_engine.create_task("长任务", ["一", "二"])
    task_engine.start_task(t["id"])
    wait_status(t["id"], "paused")
    task_engine.cancel_task(t["id"])
    assert task_engine.get_task(t["id"])["status"] == "canceled"


def test_recover_turns_running_into_paused():
    t = task_engine.create_task("恢复测试", ["一"])
    conn = db.get_db()
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (t["id"],))
    conn.commit()
    task_engine.recover()
    assert task_engine.get_task(t["id"])["status"] == "paused"


def test_unknown_tool_fails():
    t = task_engine.create_task("坏任务", [
        {"title": "调不存在的工具", "tool": "no.such.tool", "args": {}},
    ])
    task_engine.start_task(t["id"])
    t = wait_status(t["id"], "failed")
    assert t["status"] == "failed"
    assert "未知工具" in t["steps"][0]["error"]


def test_render_args_references_previous_steps():
    task = {"title": "T", "steps": [
        {"title": "一", "status": "done", "result": "第一步结果"},
        {"title": "二", "status": "done", "result": "第二步结果"},
    ]}
    out = task_engine._render_args(
        {"content": "{{last}} / {{step.1.result}} / {{task.title}}"}, task)
    assert out["content"] == "第二步结果 / 第一步结果 / T"


def test_llm_step_uses_model(monkeypatch):
    from app import llm_caps
    monkeypatch.setattr(llm_caps, "get_api_conf",
                        lambda provider: ("key", "http://x", "model"))
    monkeypatch.setattr(llm_caps, "_quick_chat",
                        lambda *a, **k: "这是模型生成的草稿")
    t = task_engine.create_task("生成", [
        {"title": "生成", "tool": "llm.generate", "args": {"prompt": "写一句话"}},
    ])
    task_engine.start_task(t["id"])
    t = wait_status(t["id"], "done")
    assert t["status"] == "done"
    assert "模型生成的草稿" in t["steps"][0]["result"]


# ============================================================
# 任务截止时间：自动同步一条日历日程，提醒复用日程链路
# ============================================================

def test_task_due_syncs_calendar_and_cancel_closes_it():
    t = task_engine.create_task("交作业", ["写"], due_at="2026-12-02 21:00",
                                remind_before=600)
    assert t["meta"]["due_at"] == calendar.parse_time("2026-12-02 21:00")
    ev = calendar.get_event(t["meta"]["event_id"])
    assert ev["title"] == "任务截止：交作业"
    assert ev["source"] == "task" and ev["source_id"] == t["id"]
    assert ev["remind_before"] == 600
    task_engine.cancel_task(t["id"])
    assert calendar.get_event(ev["id"])["status"] == "done"


def test_delete_task_removes_linked_event():
    t = task_engine.create_task("交作业", ["写"], due_at="2026-12-03 21:00")
    eid = t["meta"]["event_id"]
    assert task_engine.delete_task(t["id"]) is True
    assert calendar.get_event(eid) is None


# ============================================================
# 失败重试 / 计划可编辑 / 优先级标签 / 产物登记
# ============================================================

def test_retry_failed_steps():
    t = task_engine.create_task("重试", [
        {"title": "查询", "tool": "calendar.list", "args": {}},
    ])
    conn = db.get_db()
    conn.execute("UPDATE task_steps SET status='failed', error='boom' "
                 "WHERE task_id=?", (t["id"],))
    conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (t["id"],))
    conn.commit()

    task_engine.retry_task(t["id"])
    t = wait_status(t["id"], "done")
    assert t["status"] == "done"
    assert t["steps"][0]["status"] == "done"
    assert t["steps"][0]["retry"] == 1


def test_step_edit_add_move_delete():
    t = task_engine.create_task("编辑", ["一", "二"])
    t = task_engine.add_step(t["id"], "三", position=1)
    assert [s["title"] for s in t["steps"]] == ["一", "三", "二"]

    sid = t["steps"][1]["id"]
    t = task_engine.update_step(t["id"], sid, title="三改")
    assert t["steps"][1]["title"] == "三改"

    t = task_engine.move_step(t["id"], sid, -1)
    assert [s["title"] for s in t["steps"]] == ["三改", "一", "二"]

    t = task_engine.delete_step(t["id"], sid)
    assert [s["title"] for s in t["steps"]] == ["一", "二"]


def test_running_task_cannot_edit_steps():
    t = task_engine.create_task("运行中", ["一"])
    conn = db.get_db()
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (t["id"],))
    conn.commit()
    with pytest.raises(ValueError):
        task_engine.add_step(t["id"], "二")


def test_task_priority_and_tags():
    t = task_engine.create_task("优先级", ["一"], priority=3, tags="工作,周报")
    assert t["priority"] == 3 and t["tags"] == "工作,周报"
    t = task_engine.update_task(t["id"], priority=1, tags=["学习"])
    assert t["priority"] == 1 and t["tags"] == "学习"


def test_delete_task_removes_artifacts():
    t = task_engine.create_task("产物", ["一"])
    conn = db.get_db()
    conn.execute(
        "INSERT INTO artifacts (id,task_id,kind,name,path,size,created_at,meta) "
        "VALUES ('a_1',?,'deliverable','x.md','x.md',10,?, '{}')",
        (t["id"], time.time()))
    conn.commit()
    assert len(task_engine.list_artifacts(task_id=t["id"])) == 1
    task_engine.delete_task(t["id"])
    assert task_engine.list_artifacts(task_id=t["id"]) == []


def test_add_artifact_filters_and_dedupes(tmp_path):
    f = tmp_path / "out.md"
    f.write_text("hello", encoding="utf-8")
    t = task_engine.create_task("产物2", ["一"])
    task_engine._add_artifact(t["id"], {"tool": "write_file",
                                        "args": {"path": str(f)}})
    task_engine._add_artifact(t["id"], {"tool": "write_file",
                                        "args": {"path": str(f)}})
    # 删除文件 / 建目录不算产物
    task_engine._add_artifact(t["id"], {"tool": "delete_file",
                                        "args": {"path": str(f)}})
    task_engine._add_artifact(t["id"], {"tool": "make_dir",
                                        "args": {"path": str(tmp_path)}})
    arts = task_engine.list_artifacts(task_id=t["id"], include_working=True)
    assert len(arts) == 1
    assert arts[0]["size"] == 5
    assert arts[0]["kind"] == "deliverable"


def test_task_due_rejects_bad_time():
    with pytest.raises(ValueError):
        task_engine.create_task("坏时间", ["写"], due_at="明天")
