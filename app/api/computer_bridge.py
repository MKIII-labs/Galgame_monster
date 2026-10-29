# -*- coding: utf-8 -*-
"""Computer Use · 扩展桥的 HTTP 端点

四个只给扩展用的端点（校验 X-Bridge-Key）：
    GET  /api/computer/bridge/poll     长轮询取命令
    POST /api/computer/bridge/result   回传执行结果
    POST /api/computer/bridge/hello    心跳 + 当前标签页
    POST /api/computer/bridge/event    上报 chrome.debugger 事件

另一个给界面用的（走正常 X-Auth-Token）：
    GET  /api/computer/bridge/status   连接状态 + 连接码
"""
from urllib.parse import parse_qs, urlparse

from app.computer import bridge


def _deny(h):
    return h._json({"error": "unauthorized"}, 403)


def _query_key(h):
    return (parse_qs(urlparse(h.path).query).get("k") or [""])[0]


def _ok(h):
    return h._host_ok() and bridge.check_key(h.headers, _query_key(h))


def api_poll(h):
    if not _ok(h):
        return _deny(h)
    qs = parse_qs(urlparse(h.path).query)
    try:
        wait = float((qs.get("wait") or ["20"])[0])
    except ValueError:
        wait = 20.0
    bridge.heartbeat()
    cmd = bridge.poll(min(25.0, max(0.0, wait)))
    return h._json(cmd or {"op": ""})


def api_result(h):
    if not _ok(h):
        return _deny(h)
    body = h._body()
    bridge.heartbeat()
    ok = bridge.post_result(body.get("id"), body.get("ok"),
                            body.get("data"), body.get("error") or "")
    return h._json({"ok": ok})


def api_hello(h):
    if not _ok(h):
        return _deny(h)
    body = h._body()
    bridge.heartbeat(body.get("url") or "", body.get("title") or "",
                     body.get("attached"))
    return h._json({"ok": True, "server": "galgame-monster"})


def api_event(h):
    if not _ok(h):
        return _deny(h)
    body = h._body()
    if body.get("method"):
        bridge.heartbeat()
        bridge.push_event(body.get("method"), body.get("params") or {})
    return h._json({"ok": True})


def api_status(h):
    """给设置页：连接状态 + 连接码（含密钥，所以只走正常鉴权）。"""
    st = bridge.status()
    st["pairing"] = bridge.pairing_string()
    st["ok"] = True
    return h._json(st)
