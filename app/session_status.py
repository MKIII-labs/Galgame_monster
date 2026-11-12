# -*- coding: utf-8 -*-
"""会话运行态：待审批 / 待回答。

只存内存（进程重启即清空）——这些状态本来就属于「当前这一轮」，
写进 sessions.json 会在崩溃后留下永远消不掉的假待办。

变化时通过常驻推送总线发一条 `session_pending`，前端据此更新会话行的小圆点，
并在用户看不到该会话时弹系统通知：
    {"type": "session_pending", "sid": "...", "kind": "approval"|"ask"|"", "count": n,
     "text": "这次要你做什么", "at": 登记时间戳（前端去重用）}
"""
import threading
import time

from app import events

KINDS = ("approval", "ask")
TTL = 15 * 60          # 超过审批 / 提问的超时时间，视为陈旧记录

_lock = threading.Lock()
_pending = {}          # sid -> {"kind": str, "n": int, "at": float}


def _publish(sid, kind, count, text="", at=0):
    obj = {"type": "session_pending", "sid": sid,
           "kind": kind if count else "", "count": count}
    if text:
        obj["text"] = text
    if at:
        obj["at"] = at
    try:
        events.publish(obj)
    except Exception:                       # noqa: BLE001  推送失败不影响主流程
        pass


def mark(sid, kind, text=""):
    """登记一个待处理交互（同一会话同类会累加计数）。

    text 是给系统通知用的一行摘要（问题题面 / 要执行的命令 / 计划目标）。
    同类重复登记时保留最新一条：后来的那条通常才是用户真正要处理的事。
    """
    s = str(sid or "").strip()
    if not s or kind not in KINDS:
        return
    summary = str(text or "").strip()[:200]
    with _lock:
        cur = _pending.get(s)
        if cur and cur.get("kind") == kind:
            cur["n"] = int(cur.get("n") or 0) + 1
            cur["at"] = time.time()
            if summary:
                cur["text"] = summary
        else:
            cur = {"kind": kind, "n": 1, "at": time.time(), "text": summary}
            _pending[s] = cur
        n = cur["n"]
        at = cur["at"]
        summary = cur.get("text") or ""
    _publish(s, kind, n, summary, at)


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
            text = cur.get("text") or ""
            at = cur.get("at") or 0
        else:
            _pending.pop(s, None)
            kind = ""
            text, at = "", 0
    _publish(s, kind or "", n, text, at)


def drop(sid):
    """一轮结束 / 中断：无论还剩几个，全部清掉。"""
    s = str(sid or "").strip()
    if not s:
        return
    with _lock:
        if _pending.pop(s, None) is None:
            return
    _publish(s, "", 0)


def refresh(sid, kind=None):
    """给等待中的交互续期（不改变计数）。

    提问卡可以配置到最长 1 小时；如果只靠 TTL，侧栏的「待回答」会在 15 分钟时
    被当成陈旧记录清掉。wait_answers 每 30 秒调一次这里，进程崩溃后仍按 TTL 过期。
    """
    s = str(sid or "").strip()
    if not s:
        return
    with _lock:
        cur = _pending.get(s)
        if not cur or (kind and cur.get("kind") != kind):
            return
        cur["at"] = time.time()


def snapshot():
    """当前所有待处理会话：{sid: "approval"|"ask"}（顺便淘汰陈旧记录）。"""
    now = time.time()
    with _lock:
        for sid in list(_pending):
            if now - float(_pending[sid].get("at") or 0) > TTL:
                _pending.pop(sid, None)
        return {sid: it["kind"] for sid, it in _pending.items() if it.get("n")}
