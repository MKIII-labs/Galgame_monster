# -*- coding: utf-8 -*-
"""Skill 管理 API（自注册）。

设置面板只通过这四个操作读写 data/skills/**：
list / save / delete / toggle。启停写 config，不动用户写的 SKILL.md。
"""
from app import skills
from app.registry import route


def _bad(h, msg, code=400):
    return h._json({"ok": False, "error": msg}, code)


def _items():
    return [{
        "id": it["id"],
        "name": it["name"],
        "description": it["description"],
        "keywords": it["keywords"],
        "enabled": skills.enabled(it["id"]),
        "text": it["text"],
        "path": it["path"],
    } for it in skills.all_skills(include_disabled=True)]


@route("/api/skills")
def _api_skills(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()

    if op == "list":
        return h._json({"ok": True, "items": _items(), "dir": skills.skills_dir()})

    if op == "save":
        sid = str(body.get("id") or "").strip()
        err = skills.save(sid, body.get("text") or "")
        if err:
            return _bad(h, err.lstrip("❌ "))
        return h._json({"ok": True, "items": _items()})

    if op == "delete":
        err = skills.delete(body.get("id") or "")
        if err:
            return _bad(h, err.lstrip("❌ "))
        return h._json({"ok": True, "items": _items()})

    if op == "toggle":
        err = skills.set_enabled(body.get("id") or "", bool(body.get("enabled")))
        if err:
            return _bad(h, err.lstrip("❌ "))
        return h._json({"ok": True, "items": _items()})

    return _bad(h, "未知操作：%s" % op)
