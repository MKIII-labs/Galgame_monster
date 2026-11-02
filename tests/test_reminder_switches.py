# -*- coding: utf-8 -*-
"""提醒开关：总开关关掉全部；细分开关分别控制应用内 / 系统通知 / 日程 / 主动聊天。"""
import app.config as config_mod
from app import calendar, scheduler


def _cfg(monkeypatch, **kw):
    cfg = {"reminders_enabled": True, "reminder_inapp_enabled": True,
           "reminder_system_enabled": True, "reminder_calendar_enabled": True,
           "auto_chat_enabled": True}
    cfg.update(kw)
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    return cfg


def _task(kind="reminder"):
    return {"id": "t1", "kind": kind, "enabled": True, "paused": False,
            "at": 100, "session_id": "s1", "text": "x"}


def test_switch_on_parsing(monkeypatch):
    _cfg(monkeypatch, reminders_enabled=False)
    assert scheduler.switch_on("reminders_enabled") is False
    _cfg(monkeypatch, reminders_enabled="off")
    assert scheduler.switch_on("reminders_enabled") is False
    _cfg(monkeypatch, reminders_enabled=True)
    assert scheduler.switch_on("reminders_enabled") is True
    _cfg(monkeypatch)
    assert scheduler.switch_on("not_a_key") is True      # 缺省 = 开


def test_master_off_stops_everything(monkeypatch):
    _cfg(monkeypatch, reminders_enabled=False)
    monkeypatch.setattr(scheduler, "list_tasks", lambda: [_task("reminder"), _task("auto_chat")])
    called = []
    monkeypatch.setattr(scheduler, "_fire_reminder", lambda *a, **k: called.append("r"))
    monkeypatch.setattr(scheduler, "_fire_auto", lambda *a, **k: called.append("a"))
    assert scheduler.tick(now=1000) == []
    assert called == []


def test_inapp_off_still_advances_without_message(monkeypatch):
    _cfg(monkeypatch, reminder_inapp_enabled=False)
    monkeypatch.setattr(scheduler, "list_tasks", lambda: [_task("reminder")])
    seen = {}

    def fake_fire(task, now, deliver=True):
        seen["deliver"] = deliver

    monkeypatch.setattr(scheduler, "_fire_reminder", fake_fire)
    assert scheduler.tick(now=1000) == ["t1"]
    assert seen["deliver"] is False


def test_auto_chat_off_skips(monkeypatch):
    _cfg(monkeypatch, auto_chat_enabled=False)
    monkeypatch.setattr(scheduler, "list_tasks", lambda: [_task("auto_chat")])
    called = []
    monkeypatch.setattr(scheduler, "_fire_auto", lambda *a, **k: called.append(1))
    assert scheduler.tick(now=1000) == []
    assert called == []


def test_calendar_off_returns_early(monkeypatch):
    _cfg(monkeypatch, reminder_calendar_enabled=False)
    assert calendar.due_reminders(now=1000) == []


def test_apply_switches_unregisters_when_system_off(monkeypatch):
    _cfg(monkeypatch, reminder_system_enabled=False)
    monkeypatch.setattr(scheduler, "list_tasks", lambda: [_task("reminder")])
    dropped = []
    monkeypatch.setattr(scheduler.notify, "unregister", lambda tid: dropped.append(tid))
    scheduler.apply_switches()
    assert dropped == ["t1"]
