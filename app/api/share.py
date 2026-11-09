# -*- coding: utf-8 -*-
"""分享接口：把前端拼好的独立 HTML 落盘，返回可打开的 /share/<id>.html。"""
from app import share
from app.registry import route


@route("/api/share/save")
def _api_share_save(h):
    body = h._body()
    item = share.save(body.get("html"), body.get("title"))
    if not item:
        return h._json({"ok": False, "error": "分享内容为空或过大"}, 400)
    return h._json({"ok": True, "id": item["id"],
                    "url": "/share/%s.html" % item["id"]})
