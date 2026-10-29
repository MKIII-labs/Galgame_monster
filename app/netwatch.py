# -*- coding: utf-8 -*-
"""命令外联审计：记录一条命令（含子进程）访问了哪些外部地址。

定位是**观察**，不是拦截：
- 不改变命令怎么跑，也不要求管理员权限；
- 只采集「非本机回环」的 TCP 远端地址，命令结束后在结果里附一行摘要；
- 采集失败一律静默（返回空），不能因为审计把命令本身搞挂。

Windows 用系统 API（Toolhelp 进程快照 + GetExtendedTcpTable），不引入第三方
依赖；其它平台暂不采集。
"""
import ctypes
import socket
import struct
import threading
from ctypes import wintypes

AF_INET = 2
AF_INET6 = 23
TH32CS_SNAPPROCESS = 0x00000002
TCP_TABLE_OWNER_PID_ALL = 5
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
POLL_SECONDS = 0.5


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _Tcp4Row(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _Tcp6Row(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _process_rows():
    """[(pid, ppid, exe)]；失败返回空。"""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    rows = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID),
                         str(entry.szExeFile)))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(ctypes.c_void_p(snap))
    return rows


def process_tree(root_pid, rows=None):
    """返回 {pid: exe}，包含 root 自己与全部后代（最多 4096 个，防环）。"""
    rows = rows if rows is not None else _process_rows()
    children = {}
    names = {}
    for pid, ppid, exe in rows:
        children.setdefault(ppid, []).append(pid)
        names[pid] = exe
    out = {int(root_pid): names.get(int(root_pid), "")}
    stack = [int(root_pid)]
    guard = 0
    while stack and guard < 4096:
        cur = stack.pop()
        for pid in children.get(cur, []):
            if pid in out:
                continue
            out[pid] = names.get(pid, "")
            stack.append(pid)
            guard += 1
    return out


def _tcp_table(af, row_cls):
    iphlp = ctypes.windll.iphlpapi
    size = wintypes.DWORD(0)
    iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, af,
                              TCP_TABLE_OWNER_PID_ALL, 0)
    if not size.value:
        return []
    buf = ctypes.create_string_buffer(size.value)
    ret = iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, af,
                                    TCP_TABLE_OWNER_PID_ALL, 0)
    if ret != 0:
        return []
    count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
    base = ctypes.addressof(buf) + ctypes.sizeof(wintypes.DWORD)
    step = ctypes.sizeof(row_cls)
    out = []
    for i in range(int(count)):
        ptr = ctypes.cast(base + i * step, ctypes.POINTER(row_cls))
        out.append(ptr.contents)
    return out


def _is_loopback(ip):
    return (ip.startswith("127.") or ip in ("::1", "0.0.0.0", "::")
            or ip.startswith("fe80:") or ip.startswith("169.254."))


def tcp_connections(tree):
    """tree: {pid: exe} → [{"exe","ip","port"}]，只保留非回环的远端连接。"""
    out = []
    for af, row_cls in ((AF_INET, _Tcp4Row), (AF_INET6, _Tcp6Row)):
        try:
            rows = _tcp_table(af, row_cls)
        except Exception:
            continue
        for row in rows:
            pid = int(row.dwOwningPid)
            if pid not in tree:
                continue
            port = socket.ntohs(int(row.dwRemotePort) & 0xFFFF)
            if not port:
                continue
            try:
                if af == AF_INET:
                    ip = socket.inet_ntoa(struct.pack("<L", int(row.dwRemoteAddr)))
                else:
                    ip = socket.inet_ntop(socket.AF_INET6, bytes(row.ucRemoteAddr))
            except Exception:
                continue
            if _is_loopback(ip):
                continue
            out.append({"exe": tree.get(pid) or "", "ip": ip, "port": port})
    return out


def summary(items, limit=6):
    """把连接列表压成一行；没有发现时返回空串（不打扰正常命令）。"""
    items = list(items or [])
    if not items:
        return ""
    parts = []
    for it in items[:limit]:
        who = ("（%s）" % it["exe"]) if it.get("exe") else ""
        parts.append("%s:%d%s" % (it["ip"], it["port"], who))
    tail = "" if len(items) <= limit else " 等 %d 个" % len(items)
    return "外部连接 %d 个：%s%s" % (len(items), "、".join(parts), tail)


class Watcher:
    """命令执行期间轮询它的进程树，记录出现过哪些外部连接。"""

    def __init__(self, root_pid, interval=POLL_SECONDS):
        self.root_pid = int(root_pid)
        self.interval = max(0.2, float(interval or POLL_SECONDS))
        self._seen = set()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        try:
            if hasattr(ctypes, "windll"):
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="netwatch")
                self._thread.start()
        except Exception:
            self._thread = None
        return self

    def _run(self):
        while not self._stop.wait(self.interval):
            self.sample()

    def sample(self):
        try:
            tree = process_tree(self.root_pid)
            if len(tree) <= 1 and not tree.get(self.root_pid):
                # 命令已经退出：没有可采的进程了
                return
            for row in tcp_connections(tree):
                self._seen.add((row["exe"], row["ip"], row["port"]))
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        return [{"exe": exe, "ip": ip, "port": port}
                for (exe, ip, port) in sorted(self._seen)]
