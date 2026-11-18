# -*- coding: utf-8 -*-
"""按消息位置分叉会话（原「存档 / 倒带」里的分支能力，独立保留）。

存档点功能已删除：撤回与「从某条回复创建分支」已经覆盖了回退 / 分叉的需求。
这里只保留 branch_at —— 以当前会话的前 upto 条消息为内容复制出一条新会话，
不依赖任何快照文件，也不在磁盘上留副本。
"""
import copy
import os
import re
import time

from app.store import DATA_DIR, drop_cache, new_id
from app.sessions import ensure_session_kb_fields

# 分叉时带走的会话字段（id / title / updated_at 由新会话自己生成）
RESTORE_FIELDS = (
    "title", "messages", "summary", "summary_upto", "kb_ids", "ai_write", "memory",
    "use_profile", "use_memory",
)

_LEGACY_SAVES_DIR = os.path.join(DATA_DIR, "saves")
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe(sid):
    return _SID_RE.sub("_", str(sid or ""))[:80]


def drop_legacy_saves(sid):
    """清理旧版本留下的存档文件（会话删除时调用）。

    新版本不再写存档；这里只负责把历史残留删掉，避免删完会话磁盘上还留一份对话副本。
    """
    s = _safe(sid)
    if not s:
        return
    legacy = os.path.join(_LEGACY_SAVES_DIR, s + ".json")
    for p in (legacy,):
        try:
            if os.path.exists(p):
                os.remove(p)
                drop_cache(p)
        except OSError:
            pass
    d = os.path.join(_LEGACY_SAVES_DIR, s)
    try:
        if os.path.isdir(d):
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    os.remove(p)
                except OSError:
                    pass
                drop_cache(p)
            os.rmdir(d)
    except OSError:
        pass


def branch_at(data, s, upto):
    """按消息位置分叉：以当前会话的前 upto 条消息为内容复制出新会话。原会话不动。

    不依赖存档点：重新发言 / 编辑 / 导入的历史都可能没有对应快照，
    因此「从这条回复处分叉」必须直接按消息切片。
    """
    msgs = s.get("messages") or []
    try:
        upto = int(upto)
    except (TypeError, ValueError):
        return None, "消息位置无效"
    upto = max(0, min(upto, len(msgs)))
    if upto <= 0:
        return None, "消息位置无效"
    base = str(s.get("title") or "对话") + " · 分支"
    exist = {str(x.get("title") or "") for x in (data.get("sessions") or [])}
    n = 1
    while ("%s%d" % (base, n)) in exist:
        n += 1
    ns = {}
    for k in RESTORE_FIELDS:
        if k in s:
            ns[k] = copy.deepcopy(s[k])
    ns["messages"] = copy.deepcopy(msgs[:upto])
    cut = int(ns.get("summary_upto") or 0)
    if cut > upto:                 # 摘要覆盖了被切掉的消息 → 清空，避免摘要指向不存在的历史
        ns["summary"] = ""
        ns["summary_upto"] = 0
    else:
        ns["summary_upto"] = cut
    ns["id"] = new_id()
    ns["title"] = "%s%d" % (base, n)
    ns["updated_at"] = time.time()
    ensure_session_kb_fields(ns)
    data["sessions"].insert(0, ns)
    data["current"] = ns["id"]
    return ns["id"], ""
