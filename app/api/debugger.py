# -*- coding: utf-8 -*-
"""调试会话 API：启动 / 断点 / 单步 / 调用栈 / 变量 / 求值 / 停止。"""
from app import debugger
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


def _err(h, e):
    return h._json({"ok": False, "error": str(e)}, 400)


@route("/api/debug/start")
def _api_debug_start(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    try:
        res = debugger.start(
            sid, sb, body.get("program") or "", body.get("args") or [],
            language=body.get("language") or "",
            breakpoints=body.get("breakpoints") or {},
            stop_on_entry=bool(body.get("stop_on_entry")))
        return h._json(res)
    except debugger.DebugError as e:
        return _err(h, e)
    except Exception as e:               # noqa: BLE001
        return _err(h, e)


@route("/api/debug/state")
def _api_debug_state(h):
    sid = _sid(h._body())
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    try:
        return h._json(debugger.state(sid))
    except Exception as e:               # noqa: BLE001
        return _err(h, e)


@route("/api/debug/breakpoints")
def _api_debug_breakpoints(h):
    body = h._body()
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    try:
        return h._json(debugger.set_breakpoints(sid, body.get("breakpoints") or {}))
    except Exception as e:               # noqa: BLE001
        return _err(h, e)


@route("/api/debug/action")
def _api_debug_action(h):
    body = h._body()
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    try:
        return h._json(debugger.action(sid, str(body.get("action") or "")))
    except Exception as e:               # noqa: BLE001
        return _err(h, e)


@route("/api/debug/evaluate")
def _api_debug_evaluate(h):
    body = h._body()
    sid = _sid(body)
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    try:
        return h._json(debugger.evaluate(
            sid, str(body.get("expression") or ""),
            str(body.get("frame_id") or "")))
    except Exception as e:               # noqa: BLE001
        return _err(h, e)


@route("/api/debug/stop")
def _api_debug_stop(h):
    sid = _sid(h._body())
    if not sid:
        return h._json({"ok": False, "error": "缺少会话 id"}, 400)
    try:
        return h._json(debugger.stop(sid))
    except Exception as e:               # noqa: BLE001
        return _err(h, e)
