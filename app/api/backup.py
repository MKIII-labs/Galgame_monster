# -*- coding: utf-8 -*-
"""全量备份 API：导出 / 预检 / 恢复（由 app.registry 自注册）

大文件友好：
- 导出走 chunked 流式响应，ZIP 边压缩边发，浏览器 / Electron 壳都能看到字节进度；
- 预检把上传的 ZIP 分块落盘，再后台校验哈希，前端轮询任务进度；
- 恢复同样先落盘再解包，不把整包读进内存。
"""
import os
import tempfile

from app import backup
from app.registry import route


def _tmp_path(suffix=".zip"):
    fd, path = tempfile.mkstemp(prefix="gm-bk-", suffix=suffix)
    os.close(fd)
    return path


def _drop(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


@route("/api/backup/export")
def _api_backup_export(h):
    name = backup.backup_name()
    h.chunked_start("application/zip", filename=name)
    try:
        # sink 直接写 chunk；客户端中断会抛错，交给 finally 收尾
        backup.export_backup_to(h.chunk)
    except Exception:                            # noqa: BLE001  连接已建立，无法再改状态码
        pass
    finally:
        h.chunked_end()


@route("/api/backup/preflight")
def _api_backup_preflight(h):
    """同步预检（小文件 / 兼容旧调用）。大文件走 /preflight/job。"""
    raw = h._body_raw()
    try:
        info = backup.inspect_backup(raw)
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": str(e)}, 400)
    return h._json({"ok": True, **info})


@route("/api/backup/preflight/job")
def _api_backup_preflight_job(h):
    """接收上传的 ZIP（分块落盘），返回后台预检任务 id。"""
    path = _tmp_path()
    try:
        got = h._body_to_file(path)
    except Exception as e:                       # noqa: BLE001
        _drop(path)
        return h._json({"ok": False, "error": "上传失败：%s" % e}, 400)
    if got <= 0:
        _drop(path)
        return h._json({"ok": False, "error": "没有收到备份文件"}, 400)
    jid = backup.start_preflight_job(path)
    return h._json({"ok": True, "id": jid, "bytes": got})


@route("/api/backup/preflight/status")
def _api_backup_preflight_status(h):
    body = h._body()
    st = backup.preflight_job_status(str(body.get("id") or ""))
    if not st:
        return h._json({"ok": False, "error": "预检任务不存在或已过期"}, 404)
    st["ok"] = True
    return h._json(st)


@route("/api/backup/restore")
def _api_backup_restore(h):
    if (h.headers.get("X-Backup-Confirm") or "").lower() != "yes":
        return h._json({"ok": False, "error": "缺少确认标记"}, 400)
    path = _tmp_path()
    try:
        got = h._body_to_file(path)
        if got <= 0:
            return h._json({"ok": False, "error": "没有收到备份文件"}, 400)
        return h._json(backup.restore_backup(path))
    except Exception as e:                       # noqa: BLE001
        return h._json({"ok": False, "error": str(e)}, 400)
    finally:
        _drop(path)
