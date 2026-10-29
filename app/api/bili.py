# -*- coding: utf-8 -*-
from app import bilibili
from app.registry import route


@route("/api/bili/status")
def _api_bili_status(h):
    """测试登录：可临时传入页面里尚未保存的 SESSDATA；只回传是否登录与昵称。"""
    body = h._body() or {}
    sessdata = str(body.get("sessdata") or "").strip() or None
    try:
        return h._json(bilibili.login_status(sessdata))
    except bilibili.BiliError as e:
        return h._json({"ok": False, "logged_in": False, "error": str(e)}, 400)
