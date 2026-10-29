# -*- coding: utf-8 -*-
"""提醒任务 / 主动聊天任务：列出、新增、删除、启停。"""

import time

from app import notify, scheduler


def _set_session_field(sid, key, value):
    """把开关状态写回会话（供刷新 / 切会话后恢复）"""
    from app.sessions import load_sessions, save_sessions, find_session
    from app.store import _lock as store_lock
    data = load_sessions()
    s = find_session(data, sid)
    if not s:
        return False
    s[key] = value
    with store_lock:
        save_sessions(data)
    return True


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
        at = scheduler.parse_at(body.get("at"))
        if at is None:
            return h._json({"ok": False, "error": "时间格式无法识别"}, 400)
        if at <= 0:
            return h._json({"ok": False, "error": "时间已过去"}, 400)
        task = scheduler.add_task(
            sid, at,
            text=str(body.get("text") or ""),
            kind=("auto_chat" if kind == "auto_chat" else "reminder"),
            repeat=str(body.get("repeat") or ""),
            title=str(body.get("title") or ""),
            gap_min=body.get("gap_min") or 40,
            gap_max=body.get("gap_max") or 120,
        )
        return h._json({"ok": True, "task": task})

    if op == "delete":
        tid = str(body.get("id") or "").strip()
        return h._json({"ok": scheduler.remove_task(tid)})

    if op == "toggle":
        tid = str(body.get("id") or "").strip()
        hit = scheduler.set_enabled(tid, bool(body.get("enabled")))
        return h._json({"ok": bool(hit), "task": hit})

    if op == "autochat":
        # 主动聊天：开关会话字段 + 重建该会话的 auto_chat 任务
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return h._json({"ok": False, "error": "缺少 session_id"}, 400)
        on = bool(body.get("enabled"))
        lo = max(1, int(body.get("gap_min") or 40))
        hi = max(lo, int(body.get("gap_max") or 120))
        ok = _set_session_field(sid, "auto_chat", on)
        for t in scheduler.list_tasks():                 # 先清旧的，避免重复
            if t.get("kind") == "auto_chat" and t.get("session_id") == sid:
                scheduler.remove_task(t["id"])
        if on:
            # 首次给 20 秒的即时反馈（而不是让你干等「最短间隔」分钟）
            scheduler.add_task(sid, time.time() + 20, kind="auto_chat",
                               gap_min=lo, gap_max=hi)
        return h._json({"ok": ok})

    if op == "autochat_test":
        # 立刻生成并发送一条（用于验证功能是否正常）
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return h._json({"ok": False, "error": "缺少 session_id"}, 400)
        text = scheduler.send_now(sid)
        return h._json({"ok": bool(text), "text": text})

    return h._json({"ok": False, "error": "未知操作"}, 400)
