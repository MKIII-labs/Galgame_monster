# -*- coding: utf-8 -*-
"""分享接口：预览 / 导出（HTML、包、长图）/ 列表 / 删除 / 导入。

旧接口 `/api/share/save` 保留（本机预览页），新前端不再使用它。
"""
import base64

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
    try:
        out = share.export(body, fmt)
    except Exception as e:                # noqa: BLE001
        return h._json({"ok": False, "error": "导出失败：%s" % e}, 500)
    return h._json(out)


@route("/api/share/list")
def _api_share_list(h):
    return h._json({"ok": True, "items": share.list_exports()})


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
