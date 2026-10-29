# -*- coding: utf-8 -*-
"""调度器单测：时间解析、任务增删、到期触发与回收。

用临时 tasks.json + 打桩系统通知，避免动真实数据与本机计划任务。
"""
import time

import pytest

from app import scheduler


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "TASKS_FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(scheduler.notify, "register", lambda *a, **k: (True, "stub"))
    monkeypatch.setattr(scheduler.notify, "unregister", lambda *a, **k: (True, "stub"))
    monkeypatch.setattr(scheduler.notify, "cleanup_payload", lambda *a, **k: None)
    yield


def test_parse_at_accepts_known_formats():
    ts = scheduler.parse_at("2030-01-01 09:00")
    assert ts and ts > time.time()
    assert scheduler.parse_at("2030-01-01T09:00") == ts
    assert scheduler.parse_at(1893459600) == 1893459600


def test_parse_at_rejects_garbage():
    assert scheduler.parse_at("") is None
    assert scheduler.parse_at("明天九点") is None
    assert scheduler.parse_at(None) is None


def test_add_list_remove():
    t = scheduler.add_task("s_x", time.time() + 3600, text="喝水", kind="reminder")
    assert any(x["id"] == t["id"] for x in scheduler.list_tasks())
    assert scheduler.remove_task(t["id"]) is True
    assert scheduler.list_tasks() == []


def test_tick_fires_due_reminder_and_retires(monkeypatch):
    appended = []
    monkeypatch.setattr(
        scheduler, "_append_message",
        lambda sid, content, auto=True: appended.append((sid, content)) or True)

    t = scheduler.add_task("s_y", time.time() - 1, text="该响了", kind="reminder")
    fired = scheduler.tick()

    assert t["id"] in fired
    assert appended and appended[0][0] == "s_y"
    assert "该响了" in appended[0][1]
    assert scheduler.list_tasks() == []        # 一次性任务触发后回收


def test_tick_skips_future_and_paused(monkeypatch):
    monkeypatch.setattr(scheduler, "_append_message",
                        lambda sid, content, auto=True: True)
    scheduler.add_task("s_z", time.time() + 3600, text="还没到")
    t2 = scheduler.add_task("s_z", time.time() - 1, text="已暂停")
    scheduler.set_enabled(t2["id"], False)

    assert scheduler.tick() == []
    assert len(scheduler.list_tasks()) == 2
