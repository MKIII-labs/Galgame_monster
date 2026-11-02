# -*- coding: utf-8 -*-
"""会话运行态：待审批 / 待回答。

只存内存（进程重启即清空）——这些状态本来就属于「当前这一轮」，
写进 sessions.json 会在崩溃后留下永远消不掉的假待办。

变化时通过常驻推送总线发一条 `session_pending`，前端据此更新会话行的小圆点：
    {"type": "session_pending", "sid": "...", "kind": "approval"|"ask"|"", "count": n}
"""
import threading
import time

from app import events

KINDS = ("approval", "ask")
TTL = 15 * 60          # 超过审批 / 提问的超时时间，视为陈旧记录

_lock = threading.Lock()
_pending = {}          # sid -> {"kind": str, "n": int, "at": float}


def _publish(sid, kind, count):
    try:
        events.publish({"type": "session_pending", "sid": sid,
                        "kind": kind if count else "", "count": count})
    except Exception:                       # noqa: BLE001  推送失败不影响主流程
        pass


def mark(sid, kind):
    """登记一个待处理交互（同一会话同类会累加计数）。"""
    s = str(sid or "").strip()
    if not s or kind not in KINDS:
        return
    with _lock:
        cur = _pending.get(s)
        if cur and cur.get("kind") == kind:
            cur["n"] = int(cur.get("n") or 0) + 1
            cur["at"] = time.time()
        else:
            cur = {"kind": kind, "n": 1, "at": time.time()}
            _pending[s] = cur
        n = cur["n"]
    _publish(s, kind, n)


def clear(sid, kind=None):
    """消掉一个待处理交互；kind 不匹配时不动（避免清错另一类）。"""
    s = str(sid or "").strip()
    if not s:
        return
    with _lock:
        cur = _pending.get(s)
        if not cur or (kind and cur.get("kind") != kind):
            return
        n = max(0, int(cur.get("n") or 0) - 1)
        if n:
            cur["n"] = n
            kind = cur["kind"]
        else:
            _pending.pop(s, None)
            kind = ""
    _publish(s, kind or "", n)


def drop(sid):
    """一轮结束 / 中断：无论还剩几个，全部清掉。"""
    s = str(sid or "").strip()
    if not s:
        return
    with _lock:
        if _pending.pop(s, None) is None:
            return
    _publish(s, "", 0)


def snapshot():
    """当前所有待处理会话：{sid: "approval"|"ask"}（顺便淘汰陈旧记录）。"""
    now = time.time()
    with _lock:
        for sid in list(_pending):
            if now - float(_pending[sid].get("at") or 0) > TTL:
                _pending.pop(sid, None)
        return {sid: it["kind"] for sid, it in _pending.items() if it.get("n")}
