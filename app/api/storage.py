# -*- coding: utf-8 -*-
"""本地存储维护：压缩 sessions.json 里的重复 / 二进制附件正文。"""
import json
import os
import shutil
import time

from app import store
from app.registry import route
from app.session_compact import compact_data
from app.sessions import SESSION_FILE, flush_sessions, load_sessions


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


@route("/api/storage/compact")
def _api_storage_compact(h):
    """dry-run / 正式压缩。只清附件正文副本，不删消息。"""
    body = h._body()
    apply = bool(body.get("apply"))
    backup = ""
    with store._lock:                 # 与流式落库共用同一把锁，避免序列化时对象被改
        data = load_sessions()
        before = _size(SESSION_FILE)
        stats = compact_data(data)
        after = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        if apply and stats.get("messages"):
            if os.path.exists(SESSION_FILE):
                backup = "%s.bak-%s" % (SESSION_FILE, time.strftime("%Y%m%d-%H%M%S"))
                shutil.copy2(SESSION_FILE, backup)
            flush_sessions(data)
            after = _size(SESSION_FILE)
    return h._json({
        "ok": True, "apply": apply, "before": before, "after": after,
        "stats": stats, "backup": backup,
    })
