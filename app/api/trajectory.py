# -*- coding: utf-8 -*-
"""轨迹 API：只读查看模型视角的事件账本。"""
from app import trajectory
from app.registry import route


def _sid(body):
    sid = str(body.get("session_id") or body.get("sid") or "").strip()
    if sid:
        return sid
    try:
        from app.sessions import load_sessions
        return str((load_sessions() or {}).get("current") or "")
    except Exception:                    # noqa: BLE001
        return ""


def _int(body, key, default):
    try:
        return int(body.get(key) if body.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


@route("/api/trajectory")
def _api_trajectory(h):
    body = h._body()
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    view = trajectory.build(
        sid, before=body.get("before") or None,
        limit_turns=max(0, min(_int(body, "limit", 30), 200)))
    return h._json({"ok": True, "view": view, "rev": trajectory.revision(sid)})


@route("/api/trajectory/rev")
def _api_trajectory_rev(h):
    """轻量版本号：文件大小 + mtime，前端「跟随最新」先比对再决定是否重读。"""
    sid = _sid(h._body())
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    return h._json({"ok": True, "rev": trajectory.revision(sid)})


@route("/api/trajectory/tool")
def _api_trajectory_tool(h):
    body = h._body()
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    res = trajectory.tool_detail(
        sid, trace_id=str(body.get("trace_id") or ""),
        call_id=str(body.get("call_id") or ""))
    return h._json(res, 200 if res.get("ok") else 404)


@route("/api/trajectory/export")
def _api_trajectory_export(h):
    body = h._body()
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    return h._json({"ok": True, "data": trajectory.export(sid)})
