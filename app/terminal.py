# -*- coding: utf-8 -*-
"""IDE 内置终端：ConPTY（pywinpty）+ 环形缓冲 + 订阅队列。

与 app/agent.py 的 run_command 是两套东西：
- run_command 是**给模型**用的一次性命令，过白名单与审批；
- 这里是**给人手敲**的交互式终端，不做命令白名单，也不暴露给模型。
"""
import atexit
import os
import queue
import re
import threading
import time
import uuid

try:
    from winpty import PtyProcess
except Exception:                       # 没装 pywinpty：面板提示安装，不影响其他功能
    PtyProcess = None

BUFFER_CHARS = 200_000      # 每个终端保留的输出上限（字符）
IDLE_SECONDS = 2 * 3600     # 空闲多久自动回收
MAX_TERMS = 4               # 同时最多几个终端
READ_SIZE = 8192
SHELL = "powershell.exe -NoLogo"     # Windows PowerShell 5.1，必定存在
# Agent 的 shell 工具用无配置启动：不加载 conda / 自定义 profile，
# 启动更快，输出也不会被用户提示符与别名干扰。
# 同时卸掉 PSReadLine：它在 ConPTY 里会对整行输入逐字重绘，
# 一次性写入长命令时会丢字符（实测 `$_.Name` 变成 `.Name`）。
SHELL_AGENT = ["powershell.exe", "-NoLogo", "-NoProfile", "-NoExit",
               "-Command", "Remove-Module PSReadLine -ErrorAction SilentlyContinue"]

_TERMS = {}
_BY_SID = {}
_LOCK = threading.RLock()
_CLEANER = None


class TerminalError(Exception):
    pass


def available():
    return PtyProcess is not None


class Terminal:
    def __init__(self, sid, sandbox, cols=120, rows=30, env=None, shell=None):
        self.sid = sid
        self.tid = "t_" + uuid.uuid4().hex[:10]
        self.cwd = sandbox
        self.started_at = time.time()
        self.last_active = time.time()
        self.alive = True
        self._buf = ""
        self._start = 0                  # _buf[0] 在整个输出流里的绝对偏移
        self._subs = []
        self._lock = threading.Lock()
        self.proc = PtyProcess.spawn(shell or SHELL, cwd=sandbox, env=env,
                                     dimensions=(max(5, int(rows)), max(20, int(cols))))
        threading.Thread(target=self._reader, name="term-" + self.tid,
                         daemon=True).start()

    # ---------------- 输出 ----------------
    def _reader(self):
        while True:
            try:
                chunk = self.proc.read(READ_SIZE)
            except EOFError:
                break
            except Exception:
                break
            if not chunk:
                break
            self._append(chunk)
        self.alive = False
        self._broadcast(None)

    def _append(self, text):
        with self._lock:
            self._buf += text
            if len(self._buf) > BUFFER_CHARS:
                cut = len(self._buf) - BUFFER_CHARS
                self._buf = self._buf[cut:]
                self._start += cut
        self._broadcast(text)

    def _broadcast(self, text):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(text)
            except queue.Full:
                pass

    def snapshot(self, since=0):
        """返回 since 之后的新增输出；已经滚出缓冲时给 truncated=True 并回吐全量。"""
        with self._lock:
            buf, start, total = self._buf, self._start, self._start + len(self._buf)
        since = max(0, int(since or 0))
        truncated = since < start
        text = buf if truncated else buf[max(0, since - start):]
        return {"text": text, "off": total, "truncated": truncated,
                "alive": self.alive, "cwd": self._cwd(buf), "tid": self.tid}

    def _cwd(self, buf):
        """从 PowerShell 提示符里读当前目录；读不到就返回启动目录。"""
        hits = re.findall(r"(?m)(?:^|\n)PS ([A-Za-z]:\\[^>\r\n]*)>", buf[-4000:])
        return hits[-1].strip() if hits else self.cwd

    def subscribe(self):
        q = queue.Queue(maxsize=512)
        with self._lock:
            self._subs.append(q)
        if not self.alive:
            q.put_nowait(None)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    # ---------------- 输入 ----------------
    def write(self, text):
        if not self.alive:
            return False
        try:
            self.proc.write(text)
        except Exception:
            return False
        self.last_active = time.time()
        return True

    def resize(self, cols, rows):
        try:
            self.proc.setwinsize(max(5, int(rows)), max(20, int(cols)))
            self.last_active = time.time()
            return True
        except Exception:
            return False

    def interrupt(self):
        """真 Ctrl+C（ConPTY 的 sendintr），不是杀进程。"""
        try:
            self.proc.sendintr()
            self.last_active = time.time()
            return True
        except Exception:
            return False

    def kill(self):
        self.alive = False
        try:
            self.proc.terminate(force=True)
        except Exception:
            pass
        self._broadcast(None)

    def clear(self):
        with self._lock:
            self._start += len(self._buf)
            self._buf = ""
        return True


def _sweep():
    """定期回收空闲 / 已退出的终端，避免进程堆积。"""
    while True:
        time.sleep(300)
        now = time.time()
        with _LOCK:
            items = list(_TERMS.values())
        for t in items:
            if not t.alive and now - t.last_active > 600:
                _drop(t)
            elif now - t.last_active > IDLE_SECONDS:
                _drop(t)


def _drop(t):
    t.kill()
    with _LOCK:
        _TERMS.pop(t.tid, None)
        if _BY_SID.get(t.sid) == t.tid:
            _BY_SID.pop(t.sid, None)


def _ensure_cleaner():
    global _CLEANER
    if _CLEANER is None:
        _CLEANER = threading.Thread(target=_sweep, name="term-cleaner", daemon=True)
        _CLEANER.start()


def start(sid, sandbox, cols=120, rows=30, force=False, env=None, shell=None):
    """取（或创建）该会话的终端。force=True 时先杀掉旧进程。

    env：给子进程的环境变量；None = 继承当前进程（IDE 面板用）。
    shell：启动命令，None = 默认 SHELL。
    Agent 的 shell 工具会传清洗过的 env + SHELL_AGENT，避免把密钥带进终端、也不加载用户 profile。
    """
    if not available():
        raise TerminalError("缺少 pywinpty，请先执行：pip install pywinpty")
    if not sid or not sandbox or not os.path.isdir(sandbox):
        raise TerminalError("沙箱目录不可用，请先选择目录")
    _ensure_cleaner()
    with _LOCK:
        old_tid = _BY_SID.get(sid)
        old = _TERMS.get(old_tid) if old_tid else None
        if old and old.alive and not force:
            old.resize(cols, rows)
            old.last_active = time.time()
            return old
        if old:
            _drop(old)
        if len(_TERMS) >= MAX_TERMS:
            raise TerminalError("同时最多打开 %d 个终端，请先关闭一个" % MAX_TERMS)
        t = Terminal(sid, sandbox, cols, rows, env=env, shell=shell)
        _TERMS[t.tid] = t
        _BY_SID[sid] = t.tid
        return t


def get(tid):
    with _LOCK:
        return _TERMS.get(str(tid or ""))


def drop_by_sid(sid):
    """按会话关掉终端（Agent 的 shell close 用）。"""
    with _LOCK:
        tid = _BY_SID.get(str(sid or ""))
    return drop(tid) if tid else False


def drop(tid):
    t = get(tid)
    if t:
        _drop(t)
        return True
    return False


def kill_all():
    with _LOCK:
        items = list(_TERMS.values())
        _TERMS.clear()
        _BY_SID.clear()
    for t in items:
        t.kill()


atexit.register(kill_all)
