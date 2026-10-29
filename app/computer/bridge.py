# -*- coding: utf-8 -*-
"""Computer Use · 浏览器扩展桥（P2）

目标：复用用户**已登录**的主浏览器。
为什么不用 --remote-debugging-port：Chrome/Edge 136 起，默认用户数据目录下这两个开关
会被直接忽略（官方安全限制）。所以走扩展：
    · 扩展用 chrome.debugger 把 CDP 命令转发到当前标签页；
    · 扩展通过本机 HTTP 长轮询与控制端通信（不需要 native messaging / 注册表）。

安全：
    · 只监听 127.0.0.1（HTTP 服务本来如此）；
    · 所有桥请求必须带 X-Bridge-Key（持久保存在 data/config.json，不是每进程随机的 TOKEN）；
    · 站点授权仍由 policy 层判断，扩展只负责执行。
"""
import secrets
import threading
import time
import uuid
from collections import deque

from app.store import DATA_DIR

CONNECT_WINDOW = 25.0          # 超过这么久没心跳就算断线

_lock = threading.RLock()
_cond = threading.Condition(_lock)
_queue = deque()               # 待扩展执行的命令
_pending = {}                  # id -> {event, ok, data, error}
_call_lock = threading.Lock()  # 同一时刻只允许一条命令在飞（单标签页）
_state = {"last_seen": 0.0, "url": "", "title": "", "attached": False}
_event_sink = None             # 由 browser.py 注册：把扩展上报的 CDP 事件喂给会话
_key_cache = {"value": "", "at": 0.0}


class BridgeError(Exception):
    """扩展未连接 / 超时 / 执行失败"""


# ============================================================
# 配对密钥
# ============================================================

def key():
    """持久配对密钥：第一次取用时生成并写回配置。"""
    now = time.time()
    if _key_cache["value"] and now - _key_cache["at"] < 1.0:
        return _key_cache["value"]
    try:
        from app.config import load_config, save_config
        cfg = load_config() or {}
        val = str(cfg.get("computer_bridge_key") or "").strip()
        if not val:
            val = secrets.token_urlsafe(24)
            cfg["computer_bridge_key"] = val
            save_config(cfg)
        _key_cache.update({"value": val, "at": now})
        return val
    except Exception:
        return ""


def check_key(headers=None, query_key=""):
    """扩展用 ?k= 传密钥（故意不用自定义头：避免每次请求多一次 CORS 预检）。"""
    want = key()
    if not want:
        return False
    got = (headers or {}).get("X-Bridge-Key") or query_key or ""
    return got == want


def base_url():
    try:
        with open(DATA_DIR + "/app_port.txt", encoding="utf-8") as f:
            port = int((f.read() or "0").strip() or 0)
    except Exception:
        port = 0
    return "http://127.0.0.1:%d" % port if port else ""


def pairing_string():
    """给用户复制到扩展设置里的一行：地址 + 密钥。"""
    return "%s#%s" % (base_url(), key())


# ============================================================
# 状态
# ============================================================

def heartbeat(url="", title="", attached=None):
    with _lock:
        _state["last_seen"] = time.time()
        if url:
            _state["url"] = str(url)[:300]
        if title:
            _state["title"] = str(title)[:200]
        if attached is not None:
            _state["attached"] = bool(attached)


def available():
    with _lock:
        return bool(_state["last_seen"]) and (time.time() - _state["last_seen"]) < CONNECT_WINDOW


def status():
    with _lock:
        last = _state["last_seen"]
        return {"connected": available(), "last_seen": last,
                "url": _state["url"], "title": _state["title"],
                "attached": _state["attached"], "base": base_url()}


def set_event_sink(fn):
    global _event_sink
    _event_sink = fn


def push_event(method, params):
    sink = _event_sink
    if sink:
        try:
            sink(method, params or {})
        except Exception:
            pass


# ============================================================
# 长轮询 / 结果
# ============================================================

def poll(wait=20.0):
    """扩展来取命令：有就立刻给，没有就挂起到 wait 秒。"""
    deadline = time.time() + max(0.0, float(wait))
    with _cond:
        while not _queue:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            _cond.wait(timeout=min(remain, 1.0))
        return _queue.popleft()


def post_result(req_id, ok, data=None, error=""):
    with _lock:
        item = _pending.pop(req_id, None)
        if not item:
            return False
        item["ok"] = bool(ok)
        item["data"] = data
        item["error"] = error or ""
        item["event"].set()
        return True


def last_seen_ms():
    with _lock:
        return int(_state["last_seen"] * 1000)


def call(op, args=None, timeout=25.0):
    """控制端 → 扩展：发一条命令并等结果。"""
    if not available():
        raise BridgeError("浏览器扩展未连接。请在扩展设置里填入连接码，并保持浏览器打开。")
    req_id = "r_" + uuid.uuid4().hex[:10]
    item = {"event": threading.Event(), "ok": None, "data": None, "error": ""}
    with _call_lock:
        with _cond:
            _pending[req_id] = item
            _queue.append({"id": req_id, "op": op, "args": args or {}})
            _cond.notify_all()
        if not item["event"].wait(timeout):
            with _lock:
                _pending.pop(req_id, None)
            raise BridgeError("扩展 %d 秒内没有响应（标签页可能被关闭或卡住）" % int(timeout))
        if not item["ok"]:
            raise BridgeError(item["error"] or "扩展执行失败")
        return item["data"]


def reset():
    """测试用：清空队列与状态。"""
    with _cond:
        _queue.clear()
        _pending.clear()
        _state.update({"last_seen": 0.0, "url": "", "title": "", "attached": False})
        _cond.notify_all()
