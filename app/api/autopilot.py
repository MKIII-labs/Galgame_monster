# -*- coding: utf-8 -*-
"""自动化目标：启动、查询、暂停、恢复、停止。"""

from app import autopilot


def _gid(body):
    gid = str(body.get("goal_id") or "").strip()
    if gid:
        return gid
    sid = str(body.get("session_id") or "").strip()
    if not sid:
        return ""
    g = autopilot.goal_for_session(sid)
    return (g or {}).get("id") or ""


def _api_autopilot(h):
    body = h._body()
    op = str(body.get("op") or "status").strip().lower()

    if op == "start":
        r = autopilot.create(
            body.get("session_id"),
            body.get("objective"),
            criteria=body.get("criteria"),
            max_turns=body.get("max_turns"),
            max_minutes=body.get("max_minutes"),
            question_policy=body.get("question_policy"),
        )
        if r.get("error"):
            return h._json({"ok": False, "error": r["error"],
                            "message": r["error"]}, 400)
        return h._json({"ok": True, "goal": r})

    if op == "status":
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            return h._json({"ok": False, "error": "缺少 session_id"}, 400)
        return h._json(autopilot.status(sid))

    if op == "list":
        return h._json({"ok": True, "goals": autopilot.list_goals()[-20:]})

    if op in ("pause", "resume", "stop"):
        gid = _gid(body)
        if not gid:
            return h._json({"ok": False, "error": "没有可操作的目标"}, 400)
        fn = {"pause": autopilot.pause,
              "resume": autopilot.resume,
              "stop": autopilot.stop}[op]
        return h._json({"ok": True, "goal": fn(gid)})

    return h._json({"ok": False, "error": "未知操作"}, 400)
