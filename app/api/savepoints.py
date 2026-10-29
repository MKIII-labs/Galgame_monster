# -*- coding: utf-8 -*-
from app.store import _lock
from app.context import current_context
from app.sessions import (ensure_session_kb_fields, find_session, load_sessions, save_sessions, slim_sessions)
from app import saves

def _api_save_list(h):
        body = h._body()
        sid = body.get("session_id") or body.get("id")
        h._json({"ok": True, "items": saves.list_saves(sid)})


def _api_save_make(h):
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id") or body.get("id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        ensure_session_kb_fields(s)
        saves.add_save(s["id"], s, label=body.get("label"), manual=True)
        h._json({"ok": True, "items": saves.list_saves(s["id"])})


def _api_save_restore(h):
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id") or body.get("id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        save_id = body.get("save_id")
        if (body.get("mode") or "overwrite").strip() == "branch":
            new_sid, err = saves.branch(data, s, save_id)
            if err:
                return h._json({"ok": False, "error": err}, 404)
            with _lock:
                save_sessions(data)
            h._json({"ok": True, "mode": "branch", "new_session_id": new_sid,
                        "sessions": slim_sessions(data), "context": current_context(data)})
            return
        ok, err = saves.restore(data, s, save_id)
        if not ok:
            return h._json({"ok": False, "error": err}, 404)
        with _lock:
            save_sessions(data)
        h._json({"ok": True, "mode": "overwrite",
                    "sessions": slim_sessions(data), "context": current_context(data)})


def _api_save_branch(h):
        """按消息位置分叉：upto = 保留的消息条数（该 AI 回复的 index + 1）"""
        body = h._body()
        data = load_sessions()
        s = find_session(data, body.get("session_id") or body.get("id"))
        if not s:
            return h._json({"ok": False, "error": "no session"}, 404)
        new_sid, err = saves.branch_at(data, s, body.get("upto"))
        if err:
            return h._json({"ok": False, "error": err}, 400)
        with _lock:
            save_sessions(data)
        h._json({"ok": True, "mode": "branch", "new_session_id": new_sid,
                    "sessions": slim_sessions(data), "context": current_context(data)})


def _api_save_delete(h):
        body = h._body()
        sid = body.get("session_id") or body.get("id")
        saves.delete_save(sid, body.get("save_id"))
        h._json({"ok": True, "items": saves.list_saves(sid)})
