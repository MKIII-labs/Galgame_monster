# -*- coding: utf-8 -*-
"""Skill 管理 API（自注册）。

设置面板通过这几个操作读写 data/skills/**：
list / save / delete / toggle / open / lint / trigger / 参考文件读写 / 导入导出。
启停写 config，不动用户写的 SKILL.md。
"""
import base64
import os

from app import skills
from app.registry import route


def _bad(h, msg, code=400):
    return h._json({"ok": False, "error": msg}, code)


def _uses():
    stats = skills.stats()
    return {k: v for k, v in stats.items() if isinstance(v, dict)}


def _items():
    used = _uses()
    out = []
    for it in skills.all_skills(include_disabled=True):
        row = used.get(it["id"]) or {}
        out.append({
            "id": it["id"],
            "name": it["name"],
            "description": it["description"],
            "keywords": it["keywords"],
            "enabled": skills.enabled(it["id"]),
            "text": it["text"],
            "path": it["path"],
            "files": it["files"],
            "uses": int(row.get("count") or 0),
            "last_used": row.get("last") or 0,
        })
    return out


def _item(sid):
    it = skills.get(sid)
    if not it:
        return None
    return {"id": it["id"], "name": it["name"], "path": it["path"],
            "files": it["files"]}


def _files(sid):
    it = skills.get(sid)
    return it["files"] if it else []


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

    if op == "lint":
        text = body.get("text")
        if text is None or text == "":
            return h._json({"ok": True, "items": skills.lint(body.get("id") or "")})
        return h._json({"ok": True,
                        "items": skills.lint_text(text, str(body.get("id") or ""))})

    if op == "trigger":
        text = str(body.get("text") or "")
        hits = skills.trigger_matches(text, limit=5)
        return h._json({"ok": True, "hint": skills.trigger_hint(text),
                        "matches": [{"id": sid, "name": name, "score": score}
                                    for sid, name, score in hits]})

    if op == "read_file":
        text = skills.read_reference(body.get("id") or "", body.get("file") or "")
        if not text:
            return _bad(h, "没有这个参考文件")
        return h._json({"ok": True, "text": text})

    if op == "save_file":
        err = skills.save_reference(body.get("id") or "", body.get("file") or "",
                                    body.get("text") or "")
        if err:
            return _bad(h, err.lstrip("❌ "))
        return h._json({"ok": True, "files": _files(body.get("id") or "")})

    if op == "delete_file":
        err = skills.delete_reference(body.get("id") or "", body.get("file") or "")
        if err:
            return _bad(h, err.lstrip("❌ "))
        return h._json({"ok": True, "files": _files(body.get("id") or "")})

    if op == "export":
        sid = str(body.get("id") or "").strip()
        try:
            data = skills.export_zip(sid)
        except ValueError as e:
            return _bad(h, str(e))
        return h._json({"ok": True, "filename": (skills.safe_id(sid) or "skill") + ".zip",
                        "data": base64.b64encode(data).decode("ascii")})

    if op == "import":
        raw = body.get("data") or ""
        try:
            data = base64.b64decode(raw)
        except (ValueError, TypeError):
            return _bad(h, "文件内容不是合法的 base64")
        sid, err = skills.import_zip(data, body.get("filename") or "")
        if err:
            return _bad(h, err.lstrip("❌ "))
        return h._json({"ok": True, "id": sid, "items": _items()})

    if op == "import_path":
        sid, err = skills.import_folder(body.get("path") or "")
        if err:
            return _bad(h, err.lstrip("❌ "))
        return h._json({"ok": True, "id": sid, "items": _items()})

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
