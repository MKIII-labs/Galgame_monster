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
import time

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


def force_foreground(hwnd):
    """更强势地把窗口带到前台（坐标动作前用）。

    SetForegroundWindow 在后台进程里经常被系统拒绝；附加输入线程 + SetFocus
    是常见的可行做法。返回最终前台是否就是目标窗口。
    """
    hwnd = int(hwnd or 0)
    if not hwnd:
        return False
    if foreground_hwnd() == hwnd:
        return True
    raise_window(hwnd)
    try:
        user32.SwitchToThisWindow(wt.HWND(hwnd), True)
    except Exception:
        pass
    tries = []
    try:
        tid_target = user32.GetWindowThreadProcessId(wt.HWND(hwnd), None)
        tid_self = kernel32.GetCurrentThreadId()
        tries.append(tid_self)
        tries.append(tid_target)
        fg = foreground_hwnd()
        if fg:
            tries.append(user32.GetWindowThreadProcessId(wt.HWND(fg), None))
        attached = []
        for tid in tries:
            if tid and tid != tid_self:
                if user32.AttachThreadInput(tid_self, tid, True):
                    attached.append(tid)
        try:
            user32.BringWindowToTop(wt.HWND(hwnd))
            user32.SetForegroundWindow(wt.HWND(hwnd))
            user32.SetFocus(wt.HWND(hwnd))
        finally:
            for tid in attached:
                user32.AttachThreadInput(tid_self, tid, False)
    except Exception:
        pass
    return foreground_hwnd() == hwnd


def raise_window(hwnd):
    """把窗口提到最前（不要求前台权限）：用于坐标动作前保证点击落在目标上。"""
    hwnd = int(hwnd or 0)
    if not hwnd:
        return False
    SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
    try:
        user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
        user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(-1), 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)   # HWND_TOPMOST
        user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(-2), 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)   # HWND_NOTOPMOST
        return True
    except Exception:
        return False


def window_at(x, y):
    """坐标点上的窗口句柄（用于校验坐标动作不会点到别的窗口）。"""
    try:
        user32.WindowFromPoint.argtypes = [wt.POINT]
        user32.WindowFromPoint.restype = wt.HWND
        pt = wt.POINT(int(x), int(y))
        return int(user32.WindowFromPoint(pt) or 0)
    except Exception:
        return 0


def window_rect(hwnd):
    """窗口矩形 (left, top, right, bottom)，屏幕物理像素；拿不到返回 None。"""
    try:
        r = wt.RECT()
        if not user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r)):
            return None
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def client_size(hwnd):
    """客户区尺寸 (w, h)；拿不到返回 None。"""
    try:
        r = wt.RECT()
        if not user32.GetClientRect(wt.HWND(int(hwnd)), ctypes.byref(r)):
            return None
        return (int(r.right), int(r.bottom))
    except Exception:
        return None


def client_origin(hwnd):
    """客户区左上角在屏幕上的位置（截图若为客户区，用它换算坐标）。"""
    try:
        pt = wt.POINT(0, 0)
        if not user32.ClientToScreen(wt.HWND(int(hwnd)), ctypes.byref(pt)):
            return None
        return (int(pt.x), int(pt.y))
    except Exception:
        return None


def root_window(hwnd):
    """取顶层窗口（坐标命中子控件时归一）。"""
    try:
        GA_ROOT = 2
        return int(user32.GetAncestor(wt.HWND(int(hwnd)), GA_ROOT) or 0)
    except Exception:
        return 0


def foreground_hwnd():
    """当前前台窗口句柄（0 = 拿不到）。"""
    try:
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def class_name(hwnd):
    """窗口类名（自绘界面常见 Qt5QWindowIcon / MMUIRenderSubWindowHW 之类）。"""
    try:
        buf = ctypes.create_unicode_buffer(256)
        n = user32.GetClassNameW(wt.HWND(int(hwnd)), buf, 256)
        return buf.value if n else ""
    except Exception:
        return ""


def set_clipboard_text(text):
    """把文本放进剪贴板（Unicode 安全，中文输入用它 + Ctrl+V）。

    返回 True 表示写入成功；失败时调用方回退 uiautomation.SendKeys。
    """
    data = str(text or "")
    if not data:
        return False
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def _open():
        return user32.OpenClipboard(wt.HWND(0))

    try:
        # 64 位下必须声明返回/参数类型，否则 GlobalAlloc 的句柄会被截断成 32 位，
        # GlobalLock 拿不到指针（表现为「剪贴板写入失败」）。
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
        user32.OpenClipboard.argtypes = [wt.HWND]
        for _ in range(5):
            if _open():
                break
            time.sleep(0.05)
        else:
            return False
        try:
            user32.EmptyClipboard()
            size = (len(data) + 1) * ctypes.sizeof(ctypes.c_wchar)
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h:
                return False
            p = kernel32.GlobalLock(h)
            if not p:
                return False
            try:
                ctypes.memmove(p, ctypes.create_unicode_buffer(data), size)
            finally:
                kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                return False
            return True
        finally:
            user32.CloseClipboard()
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
