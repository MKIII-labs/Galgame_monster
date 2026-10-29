# -*- coding: utf-8 -*-
"""右键菜单 → 运行中的实例：把一个「待打开的目录」交给前端。

为什么不直接 HTTP 调用运行中的服务：``X-Auth-Token`` 是每进程随机的（httpd.TOKEN），
第二次启动的 main.py 拿不到，POST 一律 403。所以走「落一个请求文件 + 服务端 SSE 推送」：

  * 冷启动：界面还没连上 SSE，``/api/state`` 会带上 ``pending_open`` 兜底；
  * 已在运行：后台线程发现请求文件后，通过 ``/api/events`` 推一条 ``open_path``。

前端消费成功后回调 ``/api/open/path``，那里负责删掉请求文件（见 api/misc.py）。
"""
import json
import os
import threading
import time

from app import events
from app.store import DATA_DIR

FILE = os.path.join(DATA_DIR, "open_request.json")
TTL = 180          # 超过这个秒数的请求视为过期：避免下次启动突然弹出一个很旧的目录
POLL = 0.8         # 轮询间隔（只做一次 os.stat，开销可忽略）
REPEAT = 3.0       # 前端没消费时，每隔多久重推一次

_lock = threading.Lock()
_started = False
_last_push = 0.0


def set_pending(path):
    """登记一个待打开目录（覆盖旧的）；返回是否写入成功。"""
    path = str(path or "").strip().strip('"').strip("'")
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        with open(FILE, "w", encoding="utf-8") as f:
            json.dump({"path": path, "ts": time.time()}, f, ensure_ascii=False)
        return True
    except OSError:
        return False


def pending():
    """读出待打开目录；文件不存在 / 已过期 / 内容坏了都返回 None（过期的顺手清掉）。"""
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or not str(obj.get("path") or "").strip():
        clear()
        return None
    try:
        age = time.time() - float(obj.get("ts") or 0)
    except (TypeError, ValueError):
        age = TTL + 1
    if age > TTL:
        clear()
        return None
    return obj


def clear():
    try:
        os.remove(FILE)
    except OSError:
        pass


def _loop():
    global _last_push
    while True:
        time.sleep(POLL)
        obj = pending()
        if not obj or events.count() <= 0:
            continue
        now = time.time()
        if now - _last_push < REPEAT:
            continue
        _last_push = now
        events.publish({"type": "open_path", "path": obj["path"]})


def start():
    """启动后台投递线程（幂等，只起一次）。"""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="open-request", daemon=True).start()
