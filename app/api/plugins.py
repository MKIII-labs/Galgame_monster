# -*- coding: utf-8 -*-
"""插件页 API（自注册）：Python 工具插件 + MCP 摘要 + 内置发现源。"""
from app import plugin_manager as pm
from app.registry import route


def _bad(h, msg, code=400):
    return h._json({"ok": False, "error": str(msg)}, code)


def _payload():
    return {
        "plugins": pm.list_installed(),
        "catalog": pm.catalog(),
        "mcp": pm.mcp_summary(),
    }


@route("/api/plugins")
def _api_plugins(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()

    if op == "list":
        return h._json({"ok": True, **_payload()})

    if op == "detail":
        item = pm.detail(body.get("id") or "")
        if not item:
            return _bad(h, "没有这个插件")
        return h._json({"ok": True, "item": item})

    if op == "master":
        pm.set_master(bool(body.get("enabled")))
        return h._json({"ok": True, **_payload(),
                        "note": "工具插件总开关已更新，重启应用后生效"})

    if op == "toggle":
        err = pm.set_enabled(body.get("id") or "", bool(body.get("enabled")))
        if err:
            return _bad(h, err)
        return h._json({"ok": True, **_payload(),
                        "note": "启停已保存，重启应用后生效"})

    if op == "install":
        source = str(body.get("source") or "").strip().lower()
        if source == "builtin":
            pid, err = pm.install_builtin(body.get("id") or "")
        elif source == "path":
            path = str(body.get("path") or "").strip()
            if path.lower().endswith(".zip"):
                pid, err = pm.install_zip(path)
            elif path.lower().endswith(".py"):
                pid, err = pm.install_file(path)
            else:
                pid, err = pm.install_dir(path)
        elif source == "zip":
            pid, err = pm.install_zip(body.get("path") or "")
        elif source == "git":
            pid, err = pm.install_git(body.get("url") or "")
        else:
            return _bad(h, "未知安装方式：%s" % source)
        if err:
            return _bad(h, err)
        return h._json({"ok": True, "id": pid, **_payload(),
                        "note": "已安装 %s，重启应用后加载" % pid})

    if op == "uninstall":
        err = pm.uninstall(body.get("id") or "")
        if err:
            return _bad(h, err)
        return h._json({"ok": True, **_payload(),
                        "note": "已卸载，重启应用后生效"})

    if op == "open_dir":
        import os
        opener = getattr(os, "startfile", None)
        if opener is None:
            return _bad(h, "当前系统不支持打开文件夹", 500)
        try:
            os.makedirs(pm.PLUGIN_DIR, exist_ok=True)
            opener(pm.PLUGIN_DIR)
        except OSError as e:
            return _bad(h, "打开文件夹失败：%s" % e, 500)
        return h._json({"ok": True, "path": pm.PLUGIN_DIR})

    if op == "mcp_master":
        pm.mcp_set_master(bool(body.get("enabled")))
        return h._json({"ok": True, **_payload()})

    if op == "mcp_toggle":
        err = pm.mcp_set_enabled(body.get("name") or "", bool(body.get("enabled")))
        if err:
            return _bad(h, err)
        return h._json({"ok": True, **_payload()})

    return _bad(h, "未知操作：%s" % op)
