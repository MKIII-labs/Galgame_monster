# -*- coding: utf-8 -*-
"""日程 API（由 app.registry 自注册，不需要改 httpd 的路由表）"""
import datetime
import time

from app import calendar
from app.registry import route


def _bad(h, msg, code=400):
    return h._json({"ok": False, "error": msg}, code)


def _day_start(ts):
    return datetime.datetime.fromtimestamp(ts).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()


@route("/api/calendar/events")
def _api_calendar(h):
    body = h._body()
    op = str(body.get("op") or "range").strip()

    if op == "range":
        start = calendar.parse_time(body.get("start_at"))
        end = calendar.parse_time(body.get("end_at"))
        now = time.time()
        if start is None:
            start = _day_start(now)
        if end is None:
            end = start + 7 * 86400
        if end <= start:
            return _bad(h, "结束时间必须晚于开始时间")
        return h._json({"ok": True, "events": calendar.list_events(start, end)})

    if op == "today":
        now = time.time()
        start = _day_start(now)
        return h._json({"ok": True, "events": calendar.list_events(start, start + 86400)})

    if op == "get":
        ev = calendar.get_event(body.get("id"))
        return h._json({"ok": bool(ev), "event": ev} if ev else
                       {"ok": False, "error": "找不到日程"})

    if op == "create":
        try:
            ev = calendar.create_event(
                body.get("title"), body.get("start_at"), body.get("end_at"),
                all_day=bool(body.get("all_day")),
                location=body.get("location") or "",
                notes=body.get("notes") or "",
                rrule=body.get("rrule") or "",
                remind_before=body.get("remind_before") or 0,
                session_id=body.get("session_id") or "")
        except ValueError as e:
            return _bad(h, str(e))
        return h._json({"ok": True, "event": ev})

    if op == "update":
        eid = str(body.get("id") or "").strip()
        if not eid:
            return _bad(h, "缺少 id")
        fields = {k: v for k, v in body.items() if k not in ("op", "id")}
        ev = calendar.update_event(eid, **fields)
        if not ev:
            return _bad(h, "找不到日程", 404)
        return h._json({"ok": True, "event": ev})

    if op == "delete":
        eid = str(body.get("id") or "").strip()
        if not eid:
            return _bad(h, "缺少 id")
        return h._json({"ok": calendar.delete_event(eid)})

    if op == "cleanup":
        return h._json({"ok": True,
                        "removed": calendar.purge_expired(body.get("before"))})

    return _bad(h, "未知操作：%s" % op)
