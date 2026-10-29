# -*- coding: utf-8 -*-
"""全量备份 API：导出 / 预检 / 恢复（由 app.registry 自注册）"""
from app import backup
from app.registry import route


@route("/api/backup/export")
def _api_backup_export(h):
    data, name = backup.export_backup()
    return h._send(200, data, "application/zip", extra_headers={
        "Content-Disposition": 'attachment; filename="%s"' % name,
    })


@route("/api/backup/preflight")
def _api_backup_preflight(h):
    raw = h._body_raw()
    try:
        info = backup.inspect_backup(raw)
    except Exception as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    return h._json({"ok": True, **info})


@route("/api/backup/restore")
def _api_backup_restore(h):
    if (h.headers.get("X-Backup-Confirm") or "").lower() != "yes":
        return h._json({"ok": False, "error": "缺少确认标记"}, 400)
    raw = h._body_raw()
    try:
        return h._json(backup.restore_backup(raw))
    except Exception as e:
        return h._json({"ok": False, "error": str(e)}, 400)
