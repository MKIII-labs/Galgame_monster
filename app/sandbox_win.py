# -*- coding: utf-8 -*-
"""Windows AppContainer 命令沙箱。

把 Agent 执行的命令放进内核级 AppContainer：
  - 沙箱根目录：读写
  - 工具链目录（Python / Node / venv 等）：读执行
  - C:\\Windows 等系统目录：读执行（系统默认）
  - 其余路径：Access Denied，需要显式授权后才能访问
  - 网络：显式授予 internetClient capability

授权（ACL）通常需要管理员权限：`ensure_acls()` 默认**不提权**——提权会弹 UAC
（Windows 安全提示），而 Trae 的命令链路不会在每次执行时弹系统授权框。
只有用户显式打开 `agent_sandbox_elevate` 时才走一次 UAC；否则授权失败就
退回普通子进程执行（仍受沙箱路径规则与审批约束）。

主进程不直接 CreateProcess，而是用「最小环境」启动 `app.sandbox_launcher`，
由它创建 AppContainer 子进程；这样 API key 等父进程环境不会带进沙箱。
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

PROFILE_NAME = "GalgameMonster.Agent"
CAP_INTERNET = "S-1-15-3-1"
_PERM_READ = "RX"
_PERM_WRITE = "M"

_ERROR_ALREADY_EXISTS = 183
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
# 内置组「所有应用程序包」：Chromium 要求 AppContainer 可访问的安装目录必须同时给它读权限
_ALL_APP_PACKAGES = "S-1-15-2-1"
_ATTR_SECURITY_CAPABILITIES = 0x00020009
_SE_GROUP_ENABLED = 0x00000004
_STARTF_USESTDHANDLES = 0x00000100
_INFINITE = 0xFFFFFFFF
_STD_INPUT = -10
_STD_OUTPUT = -11
_STD_ERROR = -12
_HANDLE_FLAG_INHERIT = 1

_advapi32 = None
_userenv = None
_kernel32 = None
_sid_lock = threading.Lock()
_sid_cache = {"str": "", "ptr": None}
_acl_state = {"failed": False, "reason": "", "ok_roots": set()}


class SandboxError(Exception):
    """AppContainer 启动 / 授权失败。"""


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wt.DWORD),
        ("Reserved", wt.DWORD),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD), ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL)]


def _load():
    """按需加载 DLL 并声明函数签名。"""
    global _advapi32, _userenv, _kernel32
    if _kernel32 is not None:
        return
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _userenv = ctypes.WinDLL("userenv", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _userenv.CreateAppContainerProfile.argtypes = [
        wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p)]
    _userenv.CreateAppContainerProfile.restype = ctypes.c_long
    _userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wt.BOOL
    _advapi32.ConvertStringSidToSidW.argtypes = [
        wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.ConvertStringSidToSidW.restype = wt.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.GetStdHandle.argtypes = [wt.DWORD]
    _kernel32.GetStdHandle.restype = wt.HANDLE
    _kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES), wt.DWORD]
    _kernel32.CreatePipe.restype = wt.BOOL
    _kernel32.SetHandleInformation.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD]
    _kernel32.SetHandleInformation.restype = wt.BOOL
    _kernel32.CreateProcessW.argtypes = [
        wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
        wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.CreateProcessW.restype = wt.BOOL
    _kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.InitializeProcThreadAttributeList.restype = wt.BOOL
    _kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wt.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.UpdateProcThreadAttribute.restype = wt.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    _kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]


def available():
    """当前机器能否使用 AppContainer（测试进程里直接关闭，避免弹 UAC）。"""
    if os.name != "nt" or "pytest" in sys.modules:
        return False
    try:
        _load()
        return True
    except OSError:
        return False


def _sid():
    """返回 (SID 字符串, SID 指针)；失败抛 SandboxError。"""
    _load()
    with _sid_lock:
        if _sid_cache["ptr"]:
            return _sid_cache["str"], _sid_cache["ptr"]
        sid = ctypes.c_void_p()
        rc = _userenv.CreateAppContainerProfile(
            PROFILE_NAME, PROFILE_NAME, "Galgame Monster Agent", None, 0,
            ctypes.byref(sid))
        if rc != 0 and (rc & 0xFFFF) != _ERROR_ALREADY_EXISTS:
            raise SandboxError("创建 AppContainer 失败（错误码 %d）" % rc)
        if not sid.value:
            rc = _userenv.DeriveAppContainerSidFromAppContainerName(
                PROFILE_NAME, ctypes.byref(sid))
            if rc != 0:
                raise SandboxError("获取 AppContainer SID 失败（错误码 %d）" % rc)
        out = wt.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out)):
            raise SandboxError("AppContainer SID 转换失败")
        try:
            text = out.value or ""
        finally:
            _kernel32.LocalFree(out)
        _sid_cache["str"], _sid_cache["ptr"] = text, sid
        return text, sid


def sid_string():
    return _sid()[0]


# ---------------------------------------------------------------- ACL 授权


def has_grant(path, sid=None):
    """这个路径的 ACL 里是否已有 AppContainer SID 的 ACE。"""
    sid = sid or sid_string()
    try:
        r = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                           timeout=30, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return sid.lower() in (r.stdout or "").lower()


def _grant_now(paths, perm, recursive=True):
    sid = sid_string()
    todo = [p for p in paths if p and os.path.exists(p) and not has_grant(p, sid)]
    for p in todo:
        cmd = ["icacls", str(p), "/grant", "*%s:(OI)(CI)%s" % (sid, perm), "/Q"]
        if recursive:
            cmd.append("/T")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                               creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return False
        if r.returncode != 0:
            return False
    return True


def _grant_elevated(paths, perm, recursive=True):
    """用一次 UAC 提权给多个路径授权；用户取消返回 False。"""
    sid = sid_string()
    lines = ["@echo off"]
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        lines.append('icacls "%s" /grant *%s:(OI)(CI)%s%s /Q'
                     % (p, sid, perm, " /T" if recursive else ""))
    if len(lines) == 1:
        return True
    fd, bat = tempfile.mkstemp(prefix="gm_grant_", suffix=".bat")
    os.close(fd)
    try:
        with open(bat, "w", encoding="mbcs", errors="replace") as f:
            f.write("\r\n".join(lines) + "\r\n")
        ps = ("Start-Process -FilePath '%s' -Verb RunAs -Wait -WindowStyle Hidden"
              % bat.replace("'", "''"))
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps], capture_output=True, text=True, timeout=1800,
            creationflags=_CREATE_NO_WINDOW)
        if r.returncode != 0:
            return False
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.remove(bat)
        except OSError:
            pass
    return all(has_grant(p, sid) for p in paths if p and os.path.exists(p))


def toolchain_paths():
    """需要读执行权限的工具链目录（Python / Node / venv / 包管理器）。"""
    out = []
    for exe in (sys.executable, shutil.which("python"), shutil.which("python3"),
                shutil.which("node"), shutil.which("npm"), shutil.which("npx"),
                shutil.which("git"), shutil.which("py")):
        if not exe:
            continue
        p = os.path.realpath(exe)
        out.append(os.path.dirname(p))
        if os.path.basename(os.path.dirname(p)).lower() in ("scripts", "bin"):
            out.append(os.path.dirname(os.path.dirname(p)))
    for key in ("CONDA_PREFIX", "VIRTUAL_ENV"):
        val = os.environ.get(key)
        if val:
            out.append(val)
    base = getattr(sys, "base_prefix", "")
    if base:
        out.append(base)
    seen, uniq = set(), []
    for p in out:
        key = os.path.normcase(os.path.realpath(p))
        if key not in seen and os.path.isdir(p):
            seen.add(key)
            uniq.append(p)
    return uniq


def _fix_electron_install(root):
    """给项目内的 Electron 安装目录补 ALL APPLICATION PACKAGES 读权限。

    Chromium 的 install_dir_access 有硬性检查：安装目录一旦出现 AppContainer 包 SID，
    就必须同时有 ALL APPLICATION PACKAGES 的访问权，否则 Electron 主进程直接
    __debugbreak（0x80000003，窗口闪退，app.log 只有 browser window exited）。

    把本项目目录选作 AI 沙箱根目录时，ensure_acls 会递归加上包 SID，正好踩中这条；
    这里补上另一半。只在本项目的 electron/dist 存在时生效，失败静默（不能拖垮命令）。
    """
    dist = os.path.join(root, "desktop", "node_modules", "electron", "dist")
    if not os.path.isdir(dist):
        return
    try:
        # icacls 会把内置组显示成「APPLICATION PACKAGE AUTHORITY\ALL APPLICATION PACKAGES」，
        # 未必是 SID 字面量，所以两种写法都要认。
        r = subprocess.run(["icacls", dist], capture_output=True, text=True, timeout=30,
                           creationflags=_CREATE_NO_WINDOW)
        probe = (r.stdout or "").lower()
        if _ALL_APP_PACKAGES.lower() in probe or "all application packages" in probe:
            return
        subprocess.run(["icacls", dist, "/grant", "*%s:(OI)(CI)(RX)" % _ALL_APP_PACKAGES,
                        "/T", "/C", "/Q"],
                       capture_output=True, text=True, timeout=900,
                       creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass


def _elevate_allowed():
    """命令执行时是否允许弹 UAC 提权。

    默认关闭：UAC 是「Windows 安全提示」弹窗的主要来源，Trae 的命令链路
    不会在每条命令前要求系统授权。需要 OS 级沙箱的用户可以显式打开
    `agent_sandbox_elevate`（每次启动最多授权一次）。
    """
    try:
        from app.config import load_config
        return bool((load_config() or {}).get("agent_sandbox_elevate", False))
    except Exception:                  # noqa: BLE001  配置读不到就按不安全的方向处理
        return False


def ensure_acls(root, elevate=None):
    """确保沙箱根（读写）与工具链（读执行）已授权。

    返回 (ok, message)。缺少授权时先试当前权限；只有显式允许提权时才走 UAC，
    否则直接返回失败，由调用方退回普通执行。
    """
    if elevate is None:
        elevate = _elevate_allowed()
    if _acl_state["failed"]:
        return False, _acl_state["reason"]

    def _fail(msg):
        _acl_state["failed"] = True
        _acl_state["reason"] = msg
        return False, msg

    try:
        sid_string()
    except SandboxError as e:
        return _fail(str(e))
    root = os.path.realpath(str(root or ""))
    if not root or not os.path.isdir(root):
        return _fail("沙箱目录不存在：%s" % (root or "（空）"))
    # 同一次运行里同一个沙箱根已经授权过就不再重查：
    # 之前每条命令都要对根 + 每个工具链目录跑一遍 icacls，首次未授权还会递归 /T（实测数分钟）。
    if root in _acl_state["ok_roots"]:
        return True, ""
    read_paths = toolchain_paths()
    if not _grant_now([root], _PERM_WRITE):
        if not elevate or not _grant_elevated([root], _PERM_WRITE):
            return _fail("没有权限给沙箱目录授权（未提权，避免 UAC 弹窗）")
    # 必须在授权沙箱根之后立刻补 Electron 的读权限：
    # 工具链授权失败会提前 return，如果放在最后，包 SID 已经加上了、补救却没跑。
    _fix_electron_install(root)
    if read_paths and not _grant_now(read_paths, _PERM_READ):
        if not elevate or not _grant_elevated(read_paths, _PERM_READ):
            return _fail("没有权限给工具链目录授权（未提权，避免 UAC 弹窗）")
    _acl_state["ok_roots"].add(root)
    return True, ""


def grant_extra(paths, perm="RX", elevate=None):
    """给沙箱外路径追加授权（越界命令获准后调用）。

    默认不提权：提权会弹 UAC，而这条路径在「用户已经批准命令」之后执行，
    再弹一个系统授权框等于二次打断。
    """
    if elevate is None:
        elevate = _elevate_allowed()
    paths = [os.path.realpath(p) for p in (paths or []) if p and os.path.exists(p)]
    if not paths:
        return True
    if _grant_now(paths, perm):
        return True
    return _grant_elevated(paths, perm) if elevate else False


# ---------------------------------------------------------------- 启动与执行


def _caps(sid, allow_network):
    caps = _SECURITY_CAPABILITIES()
    caps.AppContainerSid = sid
    caps.Reserved = 0
    if not allow_network:
        caps.Capabilities = None
        caps.CapabilityCount = 0
        return caps
    cap_sid = ctypes.c_void_p()
    if not _advapi32.ConvertStringSidToSidW(CAP_INTERNET, ctypes.byref(cap_sid)):
        caps.Capabilities = None
        caps.CapabilityCount = 0
        return caps
    arr = (_SID_AND_ATTRIBUTES * 1)()
    arr[0].Sid = cap_sid
    arr[0].Attributes = _SE_GROUP_ENABLED
    caps.Capabilities = ctypes.cast(arr, ctypes.c_void_p)
    caps.CapabilityCount = 1
    caps._keep = (arr, cap_sid)            # 保引用到 CreateProcess 返回
    return caps


def _attr_list(caps):
    size = ctypes.c_size_t(0)
    _kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    if not _kernel32.InitializeProcThreadAttributeList(
            attrs, 1, 0, ctypes.byref(size)):
        raise SandboxError("初始化沙箱属性失败")
    if not _kernel32.UpdateProcThreadAttribute(
            attrs, 0, _ATTR_SECURITY_CAPABILITIES, ctypes.byref(caps),
            ctypes.sizeof(caps), None, None):
        raise SandboxError("写入沙箱 capability 失败")
    return attrs


def run_direct(argv, shell_cmd=None, cwd="", allow_network=True):
    """在 AppContainer 里执行一条命令，输出直通本进程的 stdout。"""
    _load()
    _, sid_ptr = _sid()
    workdir = os.path.realpath(cwd or os.getcwd())
    if not os.path.isdir(workdir):
        raise SandboxError("工作目录不存在：%s" % workdir)
    if shell_cmd is not None:
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "cmd.exe")
        cmdline = subprocess.list2cmdline([exe, "/c", str(shell_cmd)])
    else:
        if not argv:
            return 2
        exe = _resolve_exe(argv[0])
        cmdline = subprocess.list2cmdline([exe] + [str(a) for a in argv[1:]])

    caps = _caps(sid_ptr, allow_network)
    attrs = _attr_list(caps)
    si = _STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
    si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
    h_in = _kernel32.GetStdHandle(_STD_INPUT)
    h_out = _kernel32.GetStdHandle(_STD_OUTPUT)
    h_err = _kernel32.GetStdHandle(_STD_ERROR)
    for h in (h_in, h_out, h_err):
        if h:
            _kernel32.SetHandleInformation(wt.HANDLE(h), _HANDLE_FLAG_INHERIT,
                                           _HANDLE_FLAG_INHERIT)
    si.StartupInfo.hStdInput = h_in
    si.StartupInfo.hStdOutput = h_out
    si.StartupInfo.hStdError = h_err
    si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
    pi = _PROCESS_INFORMATION()
    cmd_buf = ctypes.create_unicode_buffer(cmdline)
    ok = _kernel32.CreateProcessW(
        exe, cmd_buf, None, None, True,
        _EXTENDED_STARTUPINFO_PRESENT | _CREATE_NO_WINDOW,
        None, workdir, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise SandboxError("沙箱内启动命令失败（错误码 %d）"
                           % ctypes.get_last_error())
    _kernel32.WaitForSingleObject(pi.hProcess, _INFINITE)
    code = wt.DWORD(0)
    _kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    _kernel32.CloseHandle(pi.hProcess)
    _kernel32.CloseHandle(pi.hThread)
    return int(code.value)


def run(argv, cwd, env=None, timeout=60, stop_event=None, on_line=None,
        shell_cmd=None, allow_network=True):
    """在 AppContainer 里执行一条命令，返回 (exit_code, output_text)。

    通过 `app.sandbox_launcher` 启动：launcher 继承主进程给的最小环境，
    再创建 AppContainer 子进程（继承 launcher 的干净环境）。
    """
    _load()
    sid_string()
    if not os.path.isdir(cwd):
        raise SandboxError("工作目录不存在：%s" % cwd)
    workdir = os.path.realpath(cwd)
    payload = {"argv": [str(a) for a in (argv or [])],
               "shell_cmd": shell_cmd, "cwd": workdir,
               "network": bool(allow_network)}
    token = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    e = dict(env) if env is not None else None
    if e is not None:
        tmp = os.path.join(workdir, ".gm-tmp")
        try:
            os.makedirs(tmp, exist_ok=True)
        except OSError:
            pass
        e["TEMP"] = tmp
        e["TMP"] = tmp
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.sandbox_launcher", token],
            cwd=root, env=e, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW)
    except OSError as ex:
        raise SandboxError("启动沙箱代理失败：%s" % ex)

    chunks = []

    def _reader():
        try:
            for raw in proc.stdout:
                text = raw.decode("utf-8", "replace")
                chunks.append(text)
                if on_line is not None:
                    try:
                        on_line(text)
                    except Exception:      # noqa: BLE001  推流失败不能影响命令
                        pass
        except OSError:
            pass

    th = threading.Thread(target=_reader, daemon=True, name="ac-reader")
    th.start()
    deadline = (time.time() + timeout) if timeout and timeout > 0 else None
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                _kill_tree(proc)
                th.join(timeout=2)
                return -1, "".join(chunks)
            if deadline is not None and time.time() >= deadline:
                _kill_tree(proc)
                th.join(timeout=2)
                raise SandboxError("命令超时（%d 秒）" % timeout)
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        th.join(timeout=3)
    return int(proc.returncode or 0), "".join(chunks)


def _kill_tree(proc):
    try:
        subprocess.run(["taskkill", "/PID", str(int(proc.pid)), "/T", "/F"],
                       capture_output=True, timeout=10,
                       creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _resolve_exe(exe):
    exe = str(exe)
    if os.path.isabs(exe):
        return exe
    found = shutil.which(exe)
    return found or exe


# ---------------------------------------------------------------- 持久 shell


SHELL_AGENT = ["powershell.exe", "-NoLogo", "-NoProfile", "-NoExit",
               "-Command", "Remove-Module PSReadLine -ErrorAction SilentlyContinue"]
_SHELL_BUFFER = 200_000


class ShellSession:
    """在 AppContainer 里的持久 shell（管道模式，接口对齐 terminal.Terminal）。

    通过 `app.sandbox_launcher` 启动：launcher 继承主进程给的最小环境，
    再用它自己的 stdin/stdout 创建 AppContainer 子进程。
    """

    def __init__(self, sid, cwd, env=None, cols=120, rows=30):
        self.sid = sid
        self.tid = "ac_" + uuid.uuid4().hex[:10]
        self.cwd = cwd
        self.alive = True
        self.last_active = time.time()
        self._buf = ""
        self._base = 0
        self._lock = threading.Lock()
        self._proc = None
        self._start(env)

    def _start(self, env):
        payload = {"argv": SHELL_AGENT, "shell_cmd": None,
                   "cwd": os.path.realpath(self.cwd), "network": True}
        token = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "app.sandbox_launcher", token],
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=_CREATE_NO_WINDOW)
        threading.Thread(target=self._reader, daemon=True,
                         name="ac-shell-" + self.tid).start()

    def _reader(self):
        try:
            for raw in self._proc.stdout:
                text = raw.decode("utf-8", "replace")
                with self._lock:
                    self._buf += text
                    if len(self._buf) > _SHELL_BUFFER:
                        cut = len(self._buf) - _SHELL_BUFFER
                        self._base += cut
                        self._buf = self._buf[cut:]
        except OSError:
            pass
        self.alive = False

    def snapshot(self, since=0):
        with self._lock:
            start = max(0, int(since or 0) - self._base)
            return {"text": self._buf[start:],
                    "off": self._base + len(self._buf),
                    "alive": self.alive, "cwd": self.cwd,
                    "truncated": int(since or 0) < self._base}

    def write(self, text):
        if not self.alive or self._proc is None:
            return False
        try:
            self._proc.stdin.write(str(text).encode("utf-8", "replace"))
            self._proc.stdin.flush()
            self.last_active = time.time()
            return True
        except (OSError, ValueError):
            return False

    def resize(self, cols, rows):
        return True

    def interrupt(self):
        self.kill()
        return True

    def kill(self):
        self.alive = False
        if self._proc is not None:
            try:
                subprocess.run(["taskkill", "/PID", str(int(self._proc.pid)),
                                "/T", "/F"], capture_output=True, timeout=10,
                               creationflags=_CREATE_NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        self._proc = None
