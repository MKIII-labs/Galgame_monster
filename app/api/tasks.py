# -*- coding: utf-8 -*-
"""提醒任务：列出、新增、删除、启停。"""

from app import notify, scheduler


def _api_tasks(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()

    if op == "list":
        return h._json({"ok": True, "tasks": scheduler.list_tasks(),
                        "system_notify": notify.available()})

    if op == "add":
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return h._json({"ok": False, "error": "缺少 session_id"}, 400)
        kind = str(body.get("kind") or "reminder").strip()
        if kind != "reminder":
            return h._json({"ok": False, "error": "未知任务类型"}, 400)
        at = scheduler.parse_at(body.get("at"))
        if at is None:
            return h._json({"ok": False, "error": "时间格式无法识别"}, 400)
        if at <= 0:
            return h._json({"ok": False, "error": "时间已过去"}, 400)
        task = scheduler.add_task(
            sid, at,
            text=str(body.get("text") or ""),
            kind="reminder",
            repeat=str(body.get("repeat") or ""),
            title=str(body.get("title") or ""),
        )
        return h._json({"ok": True, "task": task})

    if op == "delete":
        tid = str(body.get("id") or "").strip()
        return h._json({"ok": scheduler.remove_task(tid)})

    if op == "toggle":
        tid = str(body.get("id") or "").strip()
        hit = scheduler.set_enabled(tid, bool(body.get("enabled")))
        return h._json({"ok": bool(hit), "task": hit})

    return h._json({"ok": False, "error": "未知操作"}, 400)
