# -*- coding: utf-8 -*-
"""Computer Use · 动作轨迹

每次动作都留一条：谁（工具）、对什么（编号 + 文本）、前后 URL。
出问题时能回答「它当时看到什么、点了哪里」，否则调试就是猜。
"""
import threading
import time
from collections import deque

_MAX = 200
_entries = deque(maxlen=_MAX)
_lock = threading.Lock()


def add(tool, args, result, url_before="", url_after=""):
    with _lock:
        _entries.append({
            "ts": round(time.time(), 3),
            "tool": tool,
            "args": args or {},
            "result": str(result or "")[:300],
            "url_before": url_before,
            "url_after": url_after,
        })


def recent(n=12):
    with _lock:
        items = list(_entries)[-max(1, int(n)):]
    if not items:
        return "（还没有动作记录）"
    lines = []
    for it in items:
        lines.append("%s · %s %s → %s"
                     % (time.strftime("%H:%M:%S", time.localtime(it["ts"])),
                        it["tool"], _args_brief(it["args"]),
                        (it["url_after"] or it["url_before"] or "")[:80]))
    return "\n".join(lines)


def _args_brief(args):
    if not args:
        return ""
    bits = []
    for k in ("action", "ref", "value", "url"):
        if args.get(k) not in (None, ""):
            v = str(args[k])
            bits.append("%s=%s" % (k, v[:40]))
    return " ".join(bits)
