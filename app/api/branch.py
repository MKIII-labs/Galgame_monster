# -*- coding: utf-8 -*-
from app.store import _lock
from app.context import current_context
from app.sessions import find_session, load_sessions, save_sessions, slim_sessions
from app import branch as branch_lib


def _api_session_branch(h):
        """按消息位置分叉：upto = 保留的消息条数（该 AI 回复的 index + 1）。"""
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id") or body.get("id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        new_sid, err = branch_lib.branch_at(data, s, body.get("upto"))
        if err:
            return h._json({"ok": False, "error": err}, 400)
        with _lock:
            save_sessions(data)
        h._json({"ok": True, "mode": "branch", "new_session_id": new_sid,
                    "sessions": slim_sessions(data), "context": current_context(data)})
