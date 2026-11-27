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
    turn = _int(body, "turn", 0)
    before = (turn + 1) if turn > 0 else (body.get("before") or None)
    view = trajectory.build(
        sid, before=before,
        limit_turns=max(0, min(_int(body, "limit", 8), 200)))
    if turn > 0:
        view["focus_turn"] = turn
    return h._json({"ok": True, "view": view, "rev": trajectory.revision(sid)})


@route("/api/trajectory/turns")
def _api_trajectory_turns(h):
    """全量轮次摘要：左侧导航不再依赖已加载的 8 轮。"""
    body = h._body()
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    res = trajectory.turn_index(
        sid, before=body.get("before") or None,
        limit=max(1, min(_int(body, "limit", 500), 2000)))
    res["ok"] = True
    res["rev"] = trajectory.revision(sid)
    return h._json(res)


@route("/api/trajectory/search")
def _api_trajectory_search(h):
    """全量轨迹搜索：扫事件账本，不再只搜已加载的 cells。"""
    body = h._body()
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    res = trajectory.search(
        sid, q=str(body.get("q") or ""),
        kinds=body.get("kinds") or None,
        tool=str(body.get("tool") or ""),
        errors=bool(body.get("errors")),
        writes=bool(body.get("writes")),
        subagent=bool(body.get("subagent")),
        limit=max(1, min(_int(body, "limit", 50), 200)),
        offset=max(0, _int(body, "offset", 0)))
    res["rev"] = trajectory.revision(sid)
    return h._json(res)


@route("/api/trajectory/cell")
def _api_trajectory_cell(h):
    """按 refs 取单条记录的完整正文（列表只回预览）。"""
    body = h._body()
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    try:
        res = trajectory.cell_detail(
            sid, refs=body.get("refs") or {}, kind=str(body.get("kind") or ""),
            seg=body.get("seg") or None,
            schema_ref=body.get("schema_ref") or 0,
            tool_name=str(body.get("tool_name") or ""),
            trace_id=str(body.get("trace_id") or ""),
            record_id=str(body.get("record_id") or ""))
    except Exception as e:               # noqa: BLE001
        return h._json({"ok": False, "error": str(e)}, 500)
    return h._json(res)


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
    fmt = str(body.get("format") or "json").lower()
    if fmt == "html":
        return h._json({"ok": True, "html": trajectory.export_html(sid)})
    return h._json({"ok": True, "data": trajectory.export(sid)})
