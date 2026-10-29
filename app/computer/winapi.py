# -*- coding: utf-8 -*-
"""Computer Use · Windows 原生接口（ctypes，不依赖 pywin32）

只包 P1 用得到的几件事：
  · 进程 exe 名（白名单判断）
  · 进程是否提权（提权窗口不能注入输入，需提前拒绝）
  · 把窗口带到前台
  · 鼠标点击 / 全局急停热键
"""
import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20
SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
WM_HOTKEY = 0x0312
VK_ESCAPE = 0x1B


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wt.DWORD)]


def exe_of(pid):
    """进程 → 可执行文件完整路径；拿不到返回空串。"""
    pid = int(pid or 0)
    if pid <= 0:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value or ""
        return ""
    finally:
        kernel32.CloseHandle(h)


def is_elevated(pid):
    """目标进程是否以管理员身份运行（是则 SendInput 会被 UIPI 静默丢弃）。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid or 0))
    if not h:
        return False
    try:
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
            return False
        try:
            info = TOKEN_ELEVATION()
            need = wt.DWORD()
            ok = advapi32.GetTokenInformation(token, TokenElevation, ctypes.byref(info),
                                              ctypes.sizeof(info), ctypes.byref(need))
            return bool(ok and info.TokenIsElevated)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(h)


def set_foreground(hwnd):
    """把窗口带到前台（失败也不抛：有些窗口不允许抢占前台）。"""
    try:
        user32.ShowWindow(wt.HWND(int(hwnd)), SW_RESTORE)
        return bool(user32.SetForegroundWindow(wt.HWND(int(hwnd))))
    except Exception:
        return False


def send_click(x, y):
    """真实鼠标点击（UIA Pattern 不可用时的兜底）。"""
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def send_move(x, y):
    user32.SetCursorPos(int(x), int(y))


def send_wheel(x, y, delta):
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(delta), 0)


def register_stop_hotkey(on_press):
    """注册全局急停热键 Ctrl+Alt+Shift+Esc（跑各自的线程）。

    返回 True 表示注册成功。已注册过则直接返回 True。
    """
    global _hotkey_thread
    if _hotkey_thread is not None and _hotkey_thread.is_alive():
        return True

    def _loop():
        ok = user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT,
                                   VK_ESCAPE)
        if not ok:
            return
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        on_press()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, 1)

    _hotkey_thread = threading.Thread(target=_loop, name="cu-stop-hotkey", daemon=True)
    _hotkey_thread.start()
    return True


_hotkey_thread = None
