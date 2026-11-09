# -*- coding: utf-8 -*-
"""工作台扩展：任务新字段、重复任务、筛选、排序、模板、专注与统计。"""
import sqlite3
import time

import pytest

from app import db, task_engine, work_extras


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.configure(tmp_path / "test.db")
    yield


def test_task_new_fields_and_filters():
    parent = task_engine.create_task("父任务", ["a"])
    t = task_engine.create_task(
        "子任务", ["b"], due_at="2026-12-01 09:00", priority=2, tags="工作,周报",
        parent_id=parent["id"], depends_on=parent["id"], repeat_rule="daily",
        estimate_min=45)
    assert t["parent_id"] == parent["id"]
    assert t["depends_on"] == parent["id"]
    assert t["repeat_rule"] == "daily"
    assert t["estimate_min"] == 45
    assert t["due_at"] > 0

    by_tag = task_engine.list_tasks(tag="周报")
    assert [x["id"] for x in by_tag] == [t["id"]]
    by_pri = task_engine.list_tasks(priority=2)
    assert [x["id"] for x in by_pri] == [t["id"]]
    assert [x["id"] for x in task_engine.list_tasks(parent_id=parent["id"])] == [t["id"]]


def test_update_task_due_and_clear():
    t = task_engine.create_task("改期", ["a"])
    t = task_engine.update_task(t["id"], due_at="2026-12-02 10:30", remind_before=300)
    assert t["due_at"] > 0
    assert t["meta"]["remind_before"] == 300

    t = task_engine.update_task(t["id"], due_at="")
    assert t["due_at"] == 0


def test_complete_task_spawns_repeat():
    t = task_engine.create_task("每日站会", ["同步"], due_at="2026-12-01 09:00",
                                repeat_rule="daily")
    res = task_engine.complete_task(t["id"])
    assert res["task"]["status"] == "done"
    nxt = res["next"]
    assert nxt and nxt["id"] != t["id"]
    assert nxt["status"] == "pending"
    assert nxt["due_at"] == pytest.approx(t["due_at"] + 86400)


def test_reorder_tasks_changes_sort_order():
    a = task_engine.create_task("A", ["a"])
    b = task_engine.create_task("B", ["b"])
    task_engine.reorder_tasks([a["id"], b["id"]])
    tasks = task_engine.list_tasks(limit=10)
    assert tasks[0]["id"] == a["id"]           # sort_order 大的在前


def test_templates_crud():
    tpl = work_extras.save_template("每周复盘", "写周报", ["收集", "写稿"],
                                    tags="周报", priority=2, estimate_min=30)
    assert tpl["name"] == "每周复盘"
    assert len(work_extras.list_templates()) == 1
    tpl2 = work_extras.save_template("改名", "写周报", ["收集"],
                                     tid=tpl["id"])
    assert tpl2["id"] == tpl["id"] and tpl2["name"] == "改名"
    assert work_extras.delete_template(tpl["id"])
    assert work_extras.list_templates() == []


def test_focus_and_summary():
    now = time.time()
    work_extras.add_focus(title="写稿", start_at=now - 25 * 60, end_at=now,
                          minutes=25)
    sessions = work_extras.list_focus(start_at=now - 3600, end_at=now + 60)
    assert len(sessions) == 1 and sessions[0]["minutes"] == 25
    st = work_extras.summary(7)
    assert st["focus_minutes"] == 25
    assert len(st["done_by_day"]) == 7
    assert len(st["focus_by_day"]) == 7


def test_migration_backfills_due_and_sort(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, session_id TEXT NOT NULL DEFAULT '',"
        "scene TEXT NOT NULL DEFAULT 'general', title TEXT NOT NULL,"
        "intent TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',"
        "created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        "started_at REAL NOT NULL DEFAULT 0, finished_at REAL NOT NULL DEFAULT 0,"
        "error TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}')")
    conn.execute(
        "INSERT INTO tasks (id,title,created_at,updated_at,meta) VALUES (?,?,?,?,?)",
        ("old", "老任务", 100, 200, '{"due_at": 1700000000}'))
    conn.commit()
    conn.close()

    db.configure(path)
    row = db.get_db().execute(
        "SELECT due_at,sort_order,priority,tags FROM tasks WHERE id='old'").fetchone()
    assert row["due_at"] == 1700000000
    assert row["sort_order"] == 200
    assert row["priority"] == 0 and row["tags"] == ""
