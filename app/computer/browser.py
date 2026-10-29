# -*- coding: utf-8 -*-
"""Computer Use · 浏览器会话管理

职责：拉起/复用一个我们自己的 Edge（独立 profile），拿到页面目标，
建 CDP 连接，负责导航与「等页面加载完」。

为什么独立 profile：
  · Chrome/Edge 136 起，默认用户数据目录不允许开远程调试（官方安全限制），
    「接管你正在用的浏览器」这条路已经封了；
  · 独立 profile 也意味着 AI 看不到你主浏览器里的 cookie —— 这是安全基线。
  · 复用自己的登录态 = 在这个窗口里登录一次，之后 cookie 留在我们自己 profile 里。
"""
import json
import os
import socket
import subprocess
import threading
import time

import requests

from app.computer import bridge
from app.computer.cdp import CDP, CDPError
from app.store import DATA_DIR

STATE_DIR = os.path.join(DATA_DIR, ".computer")
PROFILE_DIR = os.path.join(STATE_DIR, "profile")
PORT_FILE = os.path.join(STATE_DIR, "port.txt")

_lock = threading.RLock()
_STATE = {"port": 0, "proc": None, "conns": {}, "current": ""}
_EXT = {"cdp": None, "opened": False}


def use_extension():
    """设置里选了扩展模式，且扩展当前在线。"""
    try:
        from app.config import load_config
        backend = str((load_config() or {}).get("computer_backend") or "local").lower()
    except Exception:
        backend = "local"
    return backend == "extension"

LAUNCH_WAIT = 25.0          # 冷启动等待上限（首次创建 profile 会慢）
NAV_WAIT = 15.0
DEFAULT_PORT = 9333         # 固定端口：端口文件丢了也能接上还活着的窗口


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _http(path, method="GET", timeout=2.5):
    port = _STATE["port"]
    if not port:
        return None
    try:
        r = requests.request(method, "http://127.0.0.1:%d%s" % (port, path), timeout=timeout)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None


def _alive(port=None):
    port = port or _STATE["port"]
    if not port:
        return False
    try:
        r = requests.get("http://127.0.0.1:%d/json/version" % port, timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def _launch(port):
    from app import webprobe
    edge = webprobe.edge_path()
    if not edge:
        raise CDPError("没找到 Edge / Chrome，无法启动受控浏览器（可设 GAL_EDGE 指向可执行文件）")
    os.makedirs(PROFILE_DIR, exist_ok=True)
    args = [edge,
            "--remote-debugging-port=%d" % port,
            "--user-data-dir=" + PROFILE_DIR,
            "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--window-size=1280,900",
            "about:blank"]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, creationflags=flags)
    deadline = time.time() + LAUNCH_WAIT
    while time.time() < deadline:
        if _alive(port):
            return proc
        if proc.poll() is not None:
            raise CDPError("浏览器启动后立即退出（退出码 %s）："
                           "可能已有一个受控浏览器窗口占着同一个配置目录，"
                           "先关掉它再试" % proc.returncode)
        time.sleep(0.2)
    raise CDPError("浏览器启动超时（%.0fs）" % LAUNCH_WAIT)


def ensure():
    """确保有一个可用的受控浏览器，返回调试端口（扩展模式返回 0）。"""
    if use_extension():
        if not bridge.available():
            raise CDPError("浏览器扩展未连接：在扩展设置里粘贴连接码，并保持浏览器打开。"
                           "（设置 → Agent → Computer Use 里可以复制）")
        return 0
    with _lock:
        if _alive():
            return _STATE["port"]
        # 端口可能来自上次运行的进程（App 重启后浏览器还活着）
        for candidate in (_read_port_file(), DEFAULT_PORT):
            if candidate and candidate != _STATE["port"] and _alive(candidate):
                _STATE["port"] = candidate
                return candidate
        port = DEFAULT_PORT if _port_free(DEFAULT_PORT) else _free_port()
        _STATE["port"] = port
        _STATE["proc"] = _launch(port)
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PORT_FILE, "w", encoding="utf-8") as f:
            f.write(str(port))
        return port


def _read_port_file():
    try:
        with open(PORT_FILE, encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def pages():
    """当前所有 page 目标（过滤掉 devtools 与空白页以外的内部页）。"""
    data = _http("/json/list")
    if not isinstance(data, list):
        return []
    out = []
    for t in data:
        if t.get("type") != "page":
            continue
        url = t.get("url") or ""
        if url.startswith(("devtools://", "edge://", "chrome://")):
            continue
        out.append(t)
    return out


def _create_page():
    t = _http("/json/new?url=about:blank", method="PUT")
    if not isinstance(t, dict):
        # 旧版内核只认 GET
        t = _http("/json/new?url=about:blank")
    if not isinstance(t, dict):
        raise CDPError("无法新建标签页")
    return t


def page_target():
    """选一个页面目标：优先上次用的，其次第一个，最后新建。"""
    ts = pages()
    if _STATE["current"]:
        for t in ts:
            if t.get("id") == _STATE["current"]:
                return t
    if ts:
        _STATE["current"] = ts[0].get("id") or ""
        return ts[0]
    t = _create_page()
    _STATE["current"] = t.get("id") or ""
    return t


def conn():
    """拿到当前页面的 CDP 连接（懒建 + 缓存）。"""
    if use_extension():
        return _ext_conn()
    with _lock:
        t = page_target()
        tid = t.get("id") or ""
        ws = t.get("webSocketDebuggerUrl") or ""
        if not ws:
            raise CDPError("页面没有调试地址（浏览器可能还没准备好）")
        old = _STATE["conns"].get(tid)
        if old is not None:
            try:
                old.send("Runtime.evaluate", {"expression": "1", "returnByValue": True}, timeout=3)
                return old
            except CDPError:
                try:
                    old.close()
                except Exception:
                    pass
                _STATE["conns"].pop(tid, None)
        c = CDP(ws, timeout=20)
        for domain in ("Page", "Runtime", "Log", "Network"):
            try:
                c.send(domain + ".enable", timeout=5)
            except CDPError:
                pass
        _STATE["conns"][tid] = c
        return c


def _ext_conn():
    """扩展模式：同一条 CDP 命令，改成由扩展用 chrome.debugger 转发。"""
    if not bridge.available():
        _EXT["cdp"] = None
        raise CDPError("浏览器扩展未连接：在扩展设置里粘贴连接码，并保持浏览器打开。")
    fresh = False
    c = _EXT["cdp"]
    if c is None:
        with _lock:
            c = _EXT["cdp"]
            if c is None:
                def rpc(method, params, timeout):
                    data = bridge.call("cdp", {"method": method, "params": params},
                                       timeout=timeout)
                    if isinstance(data, dict) and data.get("error"):
                        raise CDPError(data["error"])
                    return (data or {}).get("result") or {}
                c = CDP(rpc=rpc)
                _EXT["cdp"] = c
                fresh = True
    if fresh:
        bridge.set_event_sink(lambda m, p: c.feed(m, p))
        for domain in ("Page", "Runtime", "Log", "Network"):
            try:
                c.send(domain + ".enable", timeout=10)
            except CDPError:
                pass
    return c


def evaluate(expr, timeout=20):
    """在页面里执行一段表达式，返回 JS 值。"""
    c = conn()
    res = c.send("Runtime.evaluate",
                 {"expression": expr, "returnByValue": True, "awaitPromise": False},
                 timeout=timeout)
    if res.get("exceptionDetails"):
        det = res["exceptionDetails"]
        desc = (det.get("exception") or {}).get("description") or det.get("text") or "脚本异常"
        raise CDPError(desc)
    return (res.get("result") or {}).get("value")


def wait_loaded(timeout=NAV_WAIT):
    """等页面加载完：先等 load 事件，再轮询 readyState（单页应用也能落地）。"""
    c = conn()
    deadline = time.time() + timeout
    c.take_event("Page.loadEventFired", timeout=min(4.0, timeout))
    while time.time() < deadline:
        try:
            state = evaluate("document.readyState", timeout=3)
        except CDPError:
            state = ""
        if state in ("interactive", "complete"):
            break
        time.sleep(0.15)
    # 给框架一帧渲染时间
    time.sleep(0.25)


def navigate(url):
    """导航到指定地址，返回 (最终 url, 标题)。

    扩展模式第一次打开会**新建标签页**（不动用户正在看的页面），后续复用同一页。
    """
    if use_extension():
        if not _EXT["opened"]:
            bridge.call("open", {"url": url}, timeout=35)
            _EXT["opened"] = True
        else:
            c = _ext_conn()
            c.clear_events()
            c.send("Page.navigate", {"url": url}, timeout=15)
        wait_loaded()
    else:
        c = conn()
        c.clear_events()
        c.send("Page.navigate", {"url": url}, timeout=15)
        wait_loaded()
    try:
        info = evaluate("JSON.stringify({u:location.href,t:document.title})")
        d = json.loads(info or "{}")
        return d.get("u") or url, d.get("t") or ""
    except Exception:
        return url, ""


def current_url():
    try:
        return evaluate("location.href") or ""
    except CDPError:
        return ""


def close_all():
    with _lock:
        for c in list(_STATE["conns"].values()):
            try:
                c.close()
            except Exception:
                pass
        _STATE["conns"].clear()
        if _EXT["cdp"] is not None:
            try:
                _EXT["cdp"].close()
            except Exception:
                pass
            _EXT["cdp"] = None
            _EXT["opened"] = False
            bridge.set_event_sink(None)


def shutdown():
    """关掉**我们自己拉起的**浏览器（复用的外部实例不动）。测试与退出时用。"""
    close_all()
    with _lock:
        proc = _STATE.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        _STATE["proc"] = None
        _STATE["port"] = 0
