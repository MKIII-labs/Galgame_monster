# -*- coding: utf-8 -*-
"""「用 Galgame Monster 打开 <目录>」→ 落成一个会话。

规则（与右键菜单约定一致）：
  * 已有会话的沙箱指向同一个目录 → 复用**最近更新**的那条，不新建；
  * 没有 → 新建会话，标题取目录名，沙箱指向该目录，并顺手打开 Agent。

目录比较用 `normcase(normpath)`：Windows 下大小写与斜杠方向不该算成两个目录。
"""
import os
import time

from app.sessions import ensure_session_kb_fields, load_sessions, save_sessions
from app.store import _lock, new_id


def norm_dir(path):
    """把 ``--open`` / ``%1`` 传进来的值收敛成已存在的目录；文件取其所在目录。"""
    p = str(path or "").strip().strip('"').strip("'")
    if not p:
        return ""
    try:
        p = os.path.abspath(os.path.expanduser(p))
    except (OSError, ValueError):
        return ""
    if os.path.isfile(p):
        p = os.path.dirname(p)
    return p if os.path.isdir(p) else ""


def _same_dir(a, b):
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except (TypeError, ValueError):
        return False


def open_dir_session(path):
    """返回 (会话, 是否新建)。``path`` 必须是 norm_dir 处理过的目录。"""
    data = load_sessions()
    newest = None
    for s in data.get("sessions") or []:
        if not s.get("sandbox") or not _same_dir(s.get("sandbox"), path):
            continue
        if newest is None or float(s.get("updated_at") or 0) > float(newest.get("updated_at") or 0):
            newest = s

    created = False
    if newest is None:
        newest = {"id": new_id(),
                  "title": os.path.basename(os.path.normpath(path)) or path,
                  "updated_at": time.time(), "messages": []}
        ensure_session_kb_fields(newest)
        newest["sandbox"] = path
        newest["agent_enabled"] = True
        data.setdefault("sessions", []).insert(0, newest)
        created = True

    data["current"] = newest["id"]
    with _lock:
        save_sessions(data)
    return newest, created
