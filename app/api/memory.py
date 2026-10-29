# -*- coding: utf-8 -*-
"""统一记忆 API（自注册）。

记忆的源在知识库 / 会话记忆；这里负责全局搜索、写回与索引刷新。
"""
from app import memory
from app.db import get_db
from app.knowledge import load_kb_index
from app.registry import route
from app.sessions import load_sessions


def _bad(h, msg, code=400):
    return h._json({"ok": False, "error": msg}, code)


def _current_sid(body):
    return str(body.get("session_id") or load_sessions().get("current") or "")


def _kb_list():
    return [{"id": k.get("id"), "name": k.get("name") or "知识库"}
            for k in (load_kb_index().get("kbs") or [])]


@route("/api/memory")
def _api_memory(h):
    body = h._body()
    op = str(body.get("op") or "search").strip()

    if op == "search":
        sources = body.get("sources")
        if not isinstance(sources, list):
            sources = None
        items = memory.search(body.get("query") or "",
                              scope=body.get("scope") or "",
                              sources=sources,
                              limit=body.get("limit") or 20)
        return h._json({"ok": True, "items": items, "stats": memory.stats(),
                        "kbs": _kb_list()})

    if op == "recall":
        from app.knowledge import recall_detail
        from app.sessions import find_session
        s = find_session(load_sessions(), _current_sid(body))
        if not s:
            return _bad(h, "会话不存在", 404)
        detail = recall_detail(s, body.get("query") or "",
                               include_kbs=body.get("include_kbs", True) is not False)
        return h._json({"ok": True, **detail})

    if op == "similar":
        try:
            hits = memory.similar(body.get("text") or "",
                                  target=body.get("target") or "auto",
                                  type=body.get("type") or "fact",
                                  session_id=_current_sid(body),
                                  kb_id=body.get("kb_id") or "",
                                  limit=body.get("limit") or 3)
        except Exception as e:
            return _bad(h, str(e))
        return h._json({"ok": True, "items": hits})

    if op == "add":
        try:
            result = memory.add_memory(
                body.get("text") or "", type=body.get("type") or "fact",
                target=body.get("target") or "auto",
                session_id=_current_sid(body),
                kb_id=body.get("kb_id") or "",
                importance=body.get("importance") or 0.6,
                tags=body.get("tags") or "")
        except ValueError as e:
            return _bad(h, str(e))
        return h._json({"ok": True, **result})

    if op == "delete":
        mid = body.get("id") or ""
        row = get_db().execute(
            "SELECT source FROM memory_items WHERE id=?", (mid,)).fetchone()
        if row and row["source"] in ("kb", "session", "note"):
            return _bad(h, "知识库/会话/笔记镜像是只读的，请到原入口删除", 403)
        return h._json({"ok": memory.forget(mid)})

    if op == "reindex":
        return h._json({"ok": True, "result": memory.reindex()})

    if op == "stats":
        return h._json({"ok": True, "stats": memory.stats(), "kbs": _kb_list()})

    return _bad(h, "未知操作：%s" % op)
