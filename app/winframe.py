# -*- coding: utf-8 -*-
"""应用窗口标题栏配色（Windows）。

Edge/Chrome 的 `--app` 应用窗口，标题栏由系统（DWM）绘制，网页的 CSS / theme-color
改不动它。这里用 DWM API 直接给窗口设置「沉浸式深色标题栏」与标题栏颜色，
让它在深色模式下不再突兀。非 Windows、找不到窗口或系统不支持时静默失败。
"""
import ctypes
import sys
import threading
import time

TITLE_KEYWORD = "Galgame_monster"          # 匹配应用窗口标题（见 web/index.html <title>）

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20        # Win10 20H1+
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19    # Win10 18985+
_DWMWA_CAPTION_COLOR = 35                  # Win11 22000+
_DWMWA_TEXT_COLOR = 36                     # Win11 22000+


def _to_colorref(hex_color):
    """#rrggbb → Windows COLORREF（0x00BBGGRR）"""
    s = str(hex_color or "").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return 0
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return 0
    return (b << 16) | (g << 8) | r


def _find_hwnd(keyword=TITLE_KEYWORD):
    """按标题关键字找可见的顶层窗口（返回 hwnd，找不到返回 0）"""
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return 0
    found = []

    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if keyword.lower() in (buf.value or "").lower():
                found.append(hwnd)
        except Exception:
            pass
        return True

    try:
        cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_cb)
        user32.EnumWindows(cb, 0)
    except Exception:
        return 0
    return found[0] if found else 0


def apply(dark, bg_hex="", title=TITLE_KEYWORD):
    """给应用窗口设置标题栏颜色；成功返回 True"""
    if not sys.platform.startswith("win"):
        return False
    hwnd = _find_hwnd(title)
    if not hwnd:
        return False
    try:
        dwm = ctypes.windll.dwmapi
        h = ctypes.c_void_p(hwnd)
        flag = ctypes.c_int(1 if dark else 0)
        ok = dwm.DwmSetWindowAttribute(h, _DWMWA_USE_IMMERSIVE_DARK_MODE,
                                       ctypes.byref(flag), ctypes.sizeof(flag))
        if ok != 0:                                   # 旧系统退回属性号 19
            dwm.DwmSetWindowAttribute(h, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
                                      ctypes.byref(flag), ctypes.sizeof(flag))
        if bg_hex:                                    # Win11 才能设任意标题栏颜色
            col = ctypes.c_uint(_to_colorref(bg_hex))
            dwm.DwmSetWindowAttribute(h, _DWMWA_CAPTION_COLOR,
                                      ctypes.byref(col), ctypes.sizeof(col))
            tcol = ctypes.c_uint(_to_colorref("#ffffff" if dark else "#1f1f1f"))
            dwm.DwmSetWindowAttribute(h, _DWMWA_TEXT_COLOR,
                                      ctypes.byref(tcol), ctypes.sizeof(tcol))
        return True
    except Exception:
        return False


def apply_async(dark, bg_hex="", retries=15, delay=0.4):
    """窗口刚启动时可能还没出现，后台重试若干次（不阻塞启动）"""
    def _run():
        for _ in range(max(1, retries)):
            if apply(dark, bg_hex):
                return
            time.sleep(delay)
    threading.Thread(target=_run, daemon=True).start()
