# -*- coding: utf-8 -*-
"""分享接口：预览 / 导出（HTML、包、长图）/ 列表 / 删除 / 导入。

旧接口 `/api/share/save` 保留（本机预览页），新前端不再使用它。
"""
import base64
import os
import subprocess

from app import share
from app.registry import route

_MAX_IMPORT_B64 = 30 * 1024 * 1024      # 导入包 base64 上限（约 22MB 原文件）


@route("/api/share/save")
def _api_share_save(h):
    body = h._body()
    item = share.save(body.get("html"), body.get("title"))
    if not item:
        return h._json({"ok": False, "error": "分享内容为空或过大"}, 400)
    return h._json({"ok": True, "id": item["id"],
                    "url": "/share/%s.html" % item["id"]})


@route("/api/share/preview")
def _api_share_preview(h):
    body = h._body()
    try:
        out = share.preview(body)
    except Exception as e:                # noqa: BLE001  预览失败不能 500 裸奔
        return h._json({"ok": False, "error": "预览生成失败：%s" % e}, 400)
    out["ok"] = True
    return h._json(out)


@route("/api/share/export")
def _api_share_export(h):
    body = h._body()
    fmt = str(body.get("format") or "html").lower()
    if fmt not in ("html", "png", "package"):
        return h._json({"ok": False, "error": "不支持的格式"}, 400)
    save_path = ""
    if body.get("save_as"):
        picked = share.ask_save_path(share.default_name(fmt), fmt)
        if picked is None:
            save_path = ""                  # 没有可用对话框：退回默认导出目录
        elif not picked:
            return h._json({"ok": False, "canceled": True, "error": "已取消保存"})
        else:
            save_path = picked
    try:
        out = share.export(body, fmt, save_path=save_path)
    except Exception as e:                # noqa: BLE001
        return h._json({"ok": False, "error": "导出失败：%s" % e}, 500)
    return h._json(out)


@route("/api/share/list")
def _api_share_list(h):
    return h._json({"ok": True, "items": share.list_exports(), "dir": share.EXPORT_DIR})


@route("/api/share/reveal")
def _api_share_reveal(h):
    """在资源管理器里定位导出文件（默认目录或用户「另存为」选择的位置）。"""
    body = h._body()
    raw = str(body.get("path") or "").strip()
    if raw:
        path = os.path.abspath(raw)
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".html", ".png", ".gmshare") or not os.path.isfile(path):
            return h._json({"ok": False, "error": "文件不存在或类型不支持"}, 400)
    else:
        path = share.export_path(body.get("name"))
        if not path:
            return h._json({"ok": False, "error": "文件不存在或名称非法"}, 404)
    if os.name != "nt":
        return h._json({"ok": False, "error": "仅 Windows 支持在资源管理器中定位"}, 501)
    try:
        subprocess.Popen('explorer /select,"%s"' % path,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                # noqa: BLE001
        return h._json({"ok": False, "error": "打开资源管理器失败：%s" % e}, 500)
    return h._json({"ok": True, "path": path, "dir": os.path.dirname(path)})


@route("/api/share/delete")
def _api_share_delete(h):
    body = h._body()
    ok = share.delete_export(body.get("name"))
    return h._json({"ok": ok, "error": "" if ok else "文件不存在或名称非法"})


@route("/api/share/import")
def _api_share_import(h):
    body = h._body()
    raw = str(body.get("data") or "")
    if not raw:
        return h._json({"ok": False, "error": "没有收到文件"}, 400)
    if len(raw) > _MAX_IMPORT_B64:
        return h._json({"ok": False, "error": "文件过大"}, 413)
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:                     # noqa: BLE001
        return h._json({"ok": False, "error": "文件内容损坏"}, 400)
    try:
        out = share.import_package(data)
    except ValueError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    except Exception as e:                # noqa: BLE001
        return h._json({"ok": False, "error": "导入失败：%s" % e}, 500)
    return h._json(out)
