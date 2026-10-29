# -*- coding: utf-8 -*-
"""Computer Use · 急停开关

桌面操作最怕「停不下来」。全局热键按一次冻结、再按一次恢复；
冻结期间所有动作直接返回，不再碰鼠标键盘。
"""
import threading

_lock = threading.Lock()
_stopped = False
_listeners = []


def is_stopped():
    with _lock:
        return _stopped


def toggle(reason=""):
    global _stopped
    with _lock:
        _stopped = not _stopped
        now = _stopped
    for fn in list(_listeners):
        try:
            fn(now, reason)
        except Exception:
            pass
    return now


def clear():
    global _stopped
    with _lock:
        _stopped = False


def on_change(fn):
    _listeners.append(fn)


def state_text():
    return "已急停（再按 Ctrl+Alt+Shift+Esc 恢复）" if is_stopped() else "运行中"
