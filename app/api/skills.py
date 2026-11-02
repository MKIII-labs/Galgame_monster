# -*- coding: utf-8 -*-
"""Skill 管理 API（自注册）。

设置面板只通过这几个操作读写 data/skills/**：
list / save / delete / toggle / open。启停写 config，不动用户写的 SKILL.md。
"""
import os

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


def _item(sid):
    it = skills.get(sid)
    if not it:
        return None
    return {"id": it["id"], "name": it["name"], "path": it["path"]}


@route("/api/skills")
def _api_skills(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()

    if op == "list":
        return h._json({"ok": True, "items": _items(), "dir": skills.skills_dir()})

    if op == "save":
        sid = str(body.get("id") or "").strip()
        # unique=True：新建。id 已被占用时自动换成 <id>-2、<id>-3……，
        # 避免默认名「新 Skill」每次都写进同一个 skill/ 目录（表现为「只能存一个」）。
        if body.get("unique"):
            sid = skills.unique_id(sid)
        err = skills.save(sid, body.get("text") or "")
        if err:
            return _bad(h, err.lstrip("❌ "))
        saved = _item(sid)
        return h._json({"ok": True, "id": sid, "item": saved, "items": _items()})

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

    if op == "open":
        # 只打开固定的 skills 目录，不接任意路径（避免变成「打开任意文件」的入口）
        opener = getattr(os, "startfile", None)
        if opener is None:
            return _bad(h, "当前系统不支持打开文件夹", 500)
        folder = skills.skills_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            opener(folder)
        except OSError as e:
            return _bad(h, "打开文件夹失败：%s" % e, 500)
        return h._json({"ok": True, "path": folder})

    return _bad(h, "未知操作：%s" % op)
