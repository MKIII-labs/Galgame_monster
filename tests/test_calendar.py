# -*- coding: utf-8 -*-
"""日程：CRUD、重复展开、冲突检测、到点提醒去重、旧提醒迁移"""
import time

import pytest

from app import calendar, db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.configure(tmp_path / "test.db")
    yield


def ts(s):
    v = calendar.parse_time(s)
    assert v is not None
    return v


def test_create_and_list():
    calendar.create_event("产品周会", "2026-10-20 09:00", "2026-10-20 10:00",
                          location="3F")
    items = calendar.list_events(ts("2026-10-20 00:00"), ts("2026-10-21 00:00"))
    assert len(items) == 1
    assert items[0]["title"] == "产品周会"
    assert items[0]["location"] == "3F"


def test_conflict_detected():
    calendar.create_event("周会", "2026-10-20 09:00", "2026-10-20 10:00")
    ev = calendar.create_event("面试", "2026-10-20 09:30", "2026-10-20 10:30")
    assert ev["_conflicts"]
    assert ev["_conflicts"][0]["title"] == "周会"


def test_weekly_recurrence_expands():
    # 2026-10-05 是周一
    calendar.create_event("站会", "2026-10-05 09:00", "2026-10-05 09:30",
                          rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR")
    items = calendar.list_events(ts("2026-10-05 00:00"), ts("2026-10-12 00:00"))
    assert [i["occurrence_at"] for i in items] == sorted(
        [i["occurrence_at"] for i in items])
    assert len(items) == 3          # 周一 / 周三 / 周五


def test_update_and_delete():
    ev = calendar.create_event("临时", "2026-10-21 14:00", "2026-10-21 15:00")
    up = calendar.update_event(ev["id"], title="改期", start_at="2026-10-21 16:00",
                               end_at="2026-10-21 17:00")
    assert up["title"] == "改期"
    assert up["start_at"] == ts("2026-10-21 16:00")
    assert calendar.delete_event(ev["id"]) is True
    assert calendar.get_event(ev["id"]) is None


def test_due_reminder_fires_once(monkeypatch):
    published = []
    monkeypatch.setattr(calendar.events_bus, "publish",
                        lambda obj: published.append(obj))
    now = 1_800_000_000.0
    calendar.create_event("提醒我", now + 600, now + 1200, remind_before=300)
    fired = calendar.due_reminders(now + 400)
    assert len(fired) == 1
    # 现在还会推 calendar_update（工作台刷新），这里只关心提醒本身
    notify = [p for p in published if p.get("type") == "notify"]
    assert notify
    # 同一次 occurrence 不重复提醒
    assert calendar.due_reminders(now + 400) == []


def test_conflict_check_can_be_skipped():
    calendar.create_event("A", "2026-10-22 09:00", "2026-10-22 10:00")
    ev = calendar.create_event("B", "2026-10-22 09:30", "2026-10-22 10:30",
                               check_conflict=False)
    assert ev["_conflicts"] == []


# ============================================================
# 任务截止日程 / 手动清理过期日程
# ============================================================

def test_bind_and_close_task_event():
    ev = calendar.bind_task_event("k_1", "交作业", "2026-10-25 21:00",
                                  remind_before=600)
    assert ev["title"] == "任务截止：交作业"
    assert ev["source"] == "task" and ev["source_id"] == "k_1"
    assert ev["end_at"] - ev["start_at"] == 60
    assert calendar.close_task_event("k_1") == 1
    assert calendar.get_event(ev["id"])["status"] == "done"


def test_delete_task_event():
    ev = calendar.bind_task_event("k_2", "交作业", "2026-10-25 21:00")
    assert calendar.delete_task_event("k_2") == 1
    assert calendar.get_event(ev["id"]) is None


def test_purge_expired_keeps_repeating():
    old = calendar.create_event("旧日程", "2026-01-01 09:00", "2026-01-01 10:00")
    rec = calendar.create_event("每周", "2026-01-01 09:00", "2026-01-01 10:00",
                                rrule="FREQ=WEEKLY")
    future = calendar.create_event("未来", "2026-12-01 09:00", "2026-12-01 10:00")
    assert calendar.purge_expired("2026-02-01 00:00") == 1
    assert calendar.get_event(old["id"]) is None
    assert calendar.get_event(rec["id"]) is not None     # 重复日程整条保留
    assert calendar.get_event(future["id"]) is not None


def test_migrate_legacy_reminder(monkeypatch):
    from app import scheduler
    fake = [{"id": "t_1", "kind": "reminder", "enabled": True, "paused": False,
             "at": time.time() + 600, "text": "交作业", "title": "提醒：交作业",
             "repeat": "", "session_id": "s1"}]
    removed = []
    monkeypatch.setattr(scheduler, "list_tasks", lambda: [dict(x) for x in fake])
    monkeypatch.setattr(scheduler, "remove_task",
                        lambda tid, unregister=True: removed.append((tid, unregister)) or True)
    monkeypatch.setattr(calendar, "_sync_legacy_notify", lambda ev: None)
    assert calendar.migrate_legacy_reminders() == 1
    row = db.get_db().execute(
        "SELECT * FROM events WHERE source='legacy_reminder'").fetchone()
    assert row["source_id"] == "t_1"
    assert row["session_id"] == "s1"
    assert removed == [("t_1", False)]
    # 工作台「日程」按 30 天范围拉取，迁移后应能直接看到
    items = calendar.list_events(time.time(), time.time() + 30 * 86400)
    assert any(i["source"] == "legacy_reminder" and i["source_id"] == "t_1"
               for i in items)
    # 幂等：再次迁移不会重复创建
    assert calendar.migrate_legacy_reminders() == 0


def test_legacy_reminder_fires_at_occurrence(monkeypatch):
    published, appended = [], []
    monkeypatch.setattr(calendar.events_bus, "publish",
                        lambda obj: published.append(obj))
    monkeypatch.setattr(calendar, "_sync_legacy_notify", lambda ev: None)
    import app.scheduler as sched
    monkeypatch.setattr(sched, "_append_message",
                        lambda sid, text: appended.append((sid, text)) or True)
    now = 1_800_000_000.0
    calendar.create_event("交作业", now, now + 60, notes="交作业",
                          source="legacy_reminder", source_id="t_1",
                          session_id="s1")
    fired = calendar.due_reminders(now + 1)
    assert len(fired) == 1
    assert appended and appended[0][0] == "s1"
    notify = [p for p in published if p.get("type") == "notify"]
    assert notify and notify[0]["kind"] == "reminder"
    row = db.get_db().execute(
        "SELECT status FROM events WHERE source_id='t_1'").fetchone()
    assert row["status"] == "done"


def test_session_event_fires_after_start(monkeypatch):
    published = []
    monkeypatch.setattr(calendar.events_bus, "publish",
                        lambda obj: published.append(obj))
    now = 1_800_000_000.0
    calendar.create_event("会议", now, now + 3600, session_id="s1")
    fired = calendar.due_reminders(now + 1)
    assert len(fired) == 1
    notify = [p for p in published if p.get("type") == "notify"]
    assert notify and notify[0]["kind"] == "calendar"


# ============================================================
# 自然语言快速添加
# ============================================================

NOW = calendar.parse_time("2026-10-20 10:00")      # 周二


def _nat(text):
    ts, title = calendar.parse_natural_time(text, now=NOW)
    return calendar.fmt_time(ts) if ts else None, title


def test_natural_time_relative_day():
    ts, title = _nat("明天 10 点 开会")
    assert ts == "2026-10-21 10:00"
    assert title == "开会"


def test_natural_time_weekday_and_clock():
    ts, title = _nat("周四 15:30 交报告")
    assert ts == "2026-10-22 15:30"
    assert title == "交报告"


def test_natural_time_month_day():
    ts, title = _nat("10月25日 9:00 体检")
    assert ts == "2026-10-25 09:00"
    assert title == "体检"


def test_natural_time_afternoon():
    ts, title = _nat("下午 3 点 面试")
    assert ts == "2026-10-20 15:00"
    assert title == "面试"


def test_natural_time_no_time_returns_none():
    ts, title = _nat("随便写点什么")
    assert ts is None
    assert title == "随便写点什么"


def test_create_quick_event():
    ev = calendar.create_quick_event("明天 10 点 开会", now=NOW)
    assert ev["title"] == "开会"
    assert ev["start_at"] == calendar.parse_time("2026-10-21 10:00")
    assert ev["end_at"] - ev["start_at"] == 3600
    assert ev["remind_before"] == 600
