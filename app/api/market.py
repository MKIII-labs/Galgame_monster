# -*- coding: utf-8 -*-
"""插件市场 API（自注册）。

只提供 DSH 生态索引的只读查询（服务端分页 + 缓存）。
本地 Skill 的增删启停仍在 设置 → Skill（/api/skills），这里不再重复一套。
"""
from app import market
from app.registry import route


@route("/api/market")
def _api_market(h):
    body = h._body()
    op = str(body.get("op") or "dsh").strip()
    if op != "dsh":
        return h._json({"ok": False, "error": "未知操作：%s" % op}, 400)
    try:
        out = market.dsh_page(
            q=body.get("q") or "",
            category=body.get("category") or "",
            page=body.get("page") or 1,
            size=body.get("size") or 24,
            force=bool(body.get("refresh")),
        )
    except RuntimeError as e:
        return h._json({"ok": False, "error": str(e)}, 502)
    return h._json({"ok": True, **out})
