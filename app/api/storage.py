# -*- coding: utf-8 -*-
"""本地存储维护：压缩会话缓存、压缩事件日志 / 清理孤儿目录、磁盘占用统计。"""
import json
import os
import shutil
import time

from app import session_event_prune as sep
from app import session_events as se
from app import sessions as sessions_mod
from app import store
from app.registry import route
from app.session_compact import compact_data
from app.sessions import flush_sessions, load_sessions

# 单次扫描最多数多少个目录项：防止超大目录把设置页卡死
_USAGE_MAX_ENTRIES = 400_000
# 启动时自动压缩事件日志的阈值（只在设置里打开开关后生效）
AUTO_PRUNE_MIN_BYTES = 256 * 1024 * 1024


def _store_size():
    """会话缓存总字节数（事件流是 append-only，不参与压缩）。"""
    d = sessions_mod._store_dir()
    total = 0
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                total += os.path.getsize(p)
    except OSError:
        pass
    return total


def _dir_size(path, budget=None):
    """递归统计目录体积；返回 (bytes, 文件数, 是否因为条目上限提前结束)。"""
    total = 0
    files = 0
    seen = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    seen += 1
                    if budget is not None and seen > budget:
                        return total, files, True
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files, False


@route("/api/storage/compact")
def _api_storage_compact(h):
    """dry-run / 正式压缩。只清附件正文副本，不删消息。"""
    body = h._body()
    apply = bool(body.get("apply"))
    backup = ""
    with store._lock:                 # 与流式落库共用同一把锁，避免序列化时对象被改
        data = load_sessions()
        before = _store_size()
        stats = compact_data(data)
        after = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        if apply and stats.get("messages"):
            d = sessions_mod._store_dir()
            if os.path.isdir(d):
                backup = "%s.bak-%s" % (d, time.strftime("%Y%m%d-%H%M%S"))
                shutil.copytree(d, backup)
            flush_sessions(data)
            after = _store_size()
    return h._json({
        "ok": True, "apply": apply, "before": before, "after": after,
        "stats": stats, "backup": backup,
    })


@route("/api/storage/usage")
def _api_storage_usage(h):
    """磁盘占用：按子目录统计。只读，不删任何东西。"""
    root = store.DATA_DIR
    rows = []
    total = 0
    partial = False
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        size, files, cut = _dir_size(p, budget=_USAGE_MAX_ENTRIES)
        total += size
        partial = partial or cut
        rows.append({"name": name, "bytes": size, "files": files})
    rows.sort(key=lambda x: x["bytes"], reverse=True)
    return h._json({"ok": True, "total": total, "dirs": rows, "partial": partial,
                    "sessions": sessions_mod.storage_stats()})


def _apply_event_prune(data, rows, backup_dir):
    """写回事件日志：逐会话加写锁，成功后把内存快照重新登记，避免后续 diff 错位。"""
    done = 0
    for row in rows:
        try:
            with se._write_lock(row["sid"]):
                if sep.apply_row(row, backup_dir):
                    done += 1
        except Exception as e:          # noqa: BLE001  单个会话失败不影响其它会话
            print("[事件压缩] %s 失败：%s" % (row.get("sid"), e))
    by_id = {str(x.get("id")): x for x in data.get("sessions") or []
             if isinstance(x, dict) and x.get("id")}
    for row in rows:
        snap = by_id.get(str(row.get("sid")))
        if snap:
            try:
                se.seed_last(snap)
            except Exception:           # noqa: BLE001
                pass
    return done


def _apply_orphans(rows, backup_dir):
    moved = 0
    for row in rows:
        try:
            if sep.apply_orphan(row, backup_dir):
                moved += 1
        except Exception as e:          # noqa: BLE001
            print("[孤儿清理] %s 失败：%s" % (row.get("sid"), e))
    return moved


def _maintenance(events=True, orphans=True, apply=False):
    """压缩事件日志 / 清理孤儿目录。apply=False 只预览。"""
    with store._lock:
        data = load_sessions()
        todo, skipped, stats = sep.plan(data) if events else ([], [], {})
        orph, _keep = sep.scan_orphans(data) if orphans else ([], [])
        out = {
            "events": stats, "event_skipped": len(skipped),
            "orphans": {"count": len(orph), "bytes": sum(r["before"] for r in orph)},
            "apply": apply, "backup": "", "done": 0, "moved": 0,
        }
        if not apply:
            return out
        backup = os.path.join(store.DATA_DIR, "event_prune_backup",
                              time.strftime("%Y%m%d-%H%M%S"))
        out["done"] = _apply_event_prune(data, todo, backup) if todo else 0
        out["moved"] = _apply_orphans(orph, backup) if orph else 0
        out["backup"] = backup if (todo or orph) else ""
    return out


@route("/api/storage/maintenance")
def _api_storage_maintenance(h):
    """事件日志压缩 + 孤儿目录清理。apply=false 只算体积。"""
    body = h._body()
    apply = bool(body.get("apply"))
    events = body.get("events", True) is not False
    orphans = body.get("orphans", True) is not False
    if apply and store.ACTIVE_STREAMS:
        return h._json({"ok": False, "error": "有会话正在生成，等它结束后再清理"})
    try:
        out = _maintenance(events=events, orphans=orphans, apply=apply)
    except Exception as e:              # noqa: BLE001
        return h._json({"ok": False, "error": "清理失败：%s" % e})
    return h._json({"ok": True, **out})


def auto_prune_if_needed():
    """启动时按需压缩（默认关闭；设置里打开 storage_auto_prune 后生效）。"""
    try:
        from app.config import load_config
        if not bool((load_config() or {}).get("storage_auto_prune")):
            return
    except Exception:                   # noqa: BLE001
        return
    try:
        st = sessions_mod.storage_stats()
    except Exception:                   # noqa: BLE001
        return
    if int(st.get("event_bytes") or 0) < AUTO_PRUNE_MIN_BYTES:
        return
    if store.ACTIVE_STREAMS:
        return
    try:
        out = _maintenance(events=True, orphans=True, apply=True)
        print("[存储] 启动自动压缩：事件日志 %d 个会话，孤儿目录 %d 个，备份 %s"
              % (out.get("done") or 0, out.get("moved") or 0, out.get("backup") or ""))
    except Exception as e:              # noqa: BLE001
        print("[存储] 启动自动压缩失败：%s" % e)
