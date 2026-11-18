# -*- coding: utf-8 -*-
"""设置中心 API：当前配置 / 校验 / 保存 / 历史 / 差异 / 回滚。

`/api/config` 保留兼容（内部同样走 settings_center.save_patch），
新前端统一用 `/api/settings`，返回结构化 errors 便于逐项提示。
"""
from app import settings_center as sc
from app.registry import route


def _err(h, errors, code=400):
    msg = "；".join("%s：%s" % (e.get("key") or "配置", e.get("message"))
                    for e in (errors or []) if e)
    return h._json({"ok": False, "error": msg or "校验失败", "errors": errors or []}, code)


@route("/api/settings")
def _api_settings(h):
    body = h._body()
    op = str(body.get("op") or "current").strip()

    if op == "current":
        cfg = sc.load_config()
        return h._json({"ok": True, "config": cfg,
                        "level": cfg.get("settings_level") or "advanced",
                        "version": sc.SETTINGS_VERSION})

    if op == "validate":
        errors = sc.validate_patch(body.get("config") or {})
        return h._json({"ok": not errors, "errors": errors})

    if op == "save":
        res = sc.save_patch(body.get("patch") or body.get("config") or {},
                            source=body.get("source") or "settings")
        if not res.get("ok"):
            return _err(h, res.get("errors") or [])
        return h._json({"ok": True, "history_id": res.get("history_id") or "",
                        "changed": res.get("changed") or []})

    if op == "history":
        return h._json({"ok": True,
                        "items": sc.history(body.get("limit") or sc.HISTORY_LIMIT),
                        "limit": sc.HISTORY_LIMIT})

    if op == "diff":
        data = sc.diff(body.get("id") or "")
        if not data:
            return _err(h, [{"key": "", "message": "找不到这个历史版本"}], 404)
        return h._json({"ok": True, "diff": data})

    if op == "rollback":
        res = sc.rollback(body.get("id") or "")
        if not res.get("ok"):
            return _err(h, res.get("errors") or [])
        return h._json({"ok": True, "config": res.get("config") or {},
                        "changed": res.get("changed") or []})

    return _err(h, [{"key": "", "message": "未知操作：%s" % op}])
