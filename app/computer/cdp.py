# -*- coding: utf-8 -*-
"""Computer Use · 最小 Chrome DevTools Protocol 客户端

为什么手写而不引 Playwright：
  · 项目没有 node_modules，也不想为「点一下按钮」引入上百兆依赖；
  · 我们只需要 Page / Runtime / Input / Network / Log 五个域；
  · 出问题时能读懂每一帧，而不是隔着两层封装猜。

线程模型：一个读线程把 WS 帧解成 JSON，按 id 分发给等待者，其余进事件队列。
"""
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from collections import deque


class CDPError(Exception):
    """协议层错误：连接断开、命令返回 error、超时"""


class _WSClosed(CDPError):
    pass


_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _parse_ws_url(url):
    """ws://127.0.0.1:9222/devtools/page/XXX → (host, port, path)"""
    raw = str(url or "")
    if not raw.startswith("ws://"):
        raise CDPError("只支持 ws:// 调试地址：%s" % raw[:60])
    rest = raw[5:]
    head, _, path = rest.partition("/")
    host, _, port = head.partition(":")
    return host or "127.0.0.1", int(port or 80), "/" + path


def encode_frame(payload, opcode=0x1, mask=True):
    """客户端 → 服务端必须掩码。payload 为 bytes。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    head = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        head.append(0x80 | n if mask else n)
    elif n < 65536:
        head.append(0x80 | 126 if mask else 126)
        head += struct.pack("!H", n)
    else:
        head.append(0x80 | 127 if mask else 127)
        head += struct.pack("!Q", n)
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    head += key
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(head) + masked


def _unmask(payload, key):
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise _WSClosed("连接已关闭")
        buf += chunk
    return buf


class _WSClient:
    """只实现 CDP 用得到的那部分：文本帧、ping/pong、分片拼接。"""

    def __init__(self, url, timeout=20):
        self.host, self.port, self.path = _parse_ws_url(url)
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET %s HTTP/1.1\r\n"
               "Host: %s:%d\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n") % (self.path, self.host, self.port, key)
        self.sock.sendall(req.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("握手失败：连接被关闭")
            data += chunk
            if len(data) > 65536:
                raise CDPError("握手失败：响应过大")
        head, _, self._buf = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if "101" not in (lines[0] or ""):
            raise CDPError("握手失败：%s" % (lines[0] or "无响应"))
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        want = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode()
        if headers.get("sec-websocket-accept") != want:
            raise CDPError("握手校验失败：Sec-WebSocket-Accept 不匹配")

    def send_text(self, text):
        self.sock.sendall(encode_frame(text, 0x1, mask=True))

    def _send_control(self, opcode, payload=b""):
        try:
            self.sock.sendall(encode_frame(payload, opcode, mask=True))
        except OSError:
            pass

    def recv_text(self):
        """读一条完整的文本消息；自动处理 ping / 分片 / close。"""
        frames = []
        opcode0 = None
        while True:
            b1, b2 = _recv_exact(self.sock, 2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", _recv_exact(self.sock, 2))[0]
            elif n == 127:
                n = struct.unpack("!Q", _recv_exact(self.sock, 8))[0]
            key = _recv_exact(self.sock, 4) if masked else b""
            payload = _recv_exact(self.sock, n) if n else b""
            if masked:
                payload = _unmask(payload, key)
            if opcode == 0x8:                       # close
                raise _WSClosed("对端关闭了调试连接")
            if opcode == 0x9:                       # ping → pong
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:                       # pong
                continue
            if opcode in (0x1, 0x2):
                opcode0 = opcode
                frames = [payload]
            elif opcode == 0x0:
                frames.append(payload)
            else:
                continue
            if fin:
                data = b"".join(frames)
                if opcode0 == 0x2:
                    continue                        # CDP 不用二进制
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self._send_control(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    """一个页面目标上的调试会话。

    两种传输：
      · ws_url：本地受控浏览器，走 WebSocket（P0）；
      · rpc：浏览器扩展桥 —— 扩展里用 chrome.debugger 转发同一条 CDP 命令（P2）。
        这样快照 / 动作 / console 的代码一行都不用改，只是「命令怎么送出去」不同。
    """

    def __init__(self, ws_url=None, timeout=20, rpc=None):
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending = {}
        self._events = deque(maxlen=1200)
        self._closed = False
        self._id = 0
        self._ws = None
        self._reader = None
        self._rpc = rpc
        if rpc is None:
            self._ws = _WSClient(ws_url, timeout=timeout)
            self._reader = threading.Thread(target=self._loop, name="cdp-reader", daemon=True)
            self._reader.start()

    # ---------------------------------------------------------- 收发
    def _loop(self):
        while not self._closed:
            try:
                raw = self._ws.recv_text()
            except Exception as e:
                with self._lock:
                    pending = list(self._pending.values())
                    self._pending.clear()
                    self._closed = True
                for item in pending:
                    item["error"] = "调试连接已断开：%s" % e
                    item["event"].set()
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self._handle(msg)

    def _handle(self, msg):
        """一条 CDP 消息：带 id 的是命令回包，其余是事件。"""
        mid = msg.get("id")
        if mid is not None:
            with self._lock:
                item = self._pending.pop(mid, None)
            if item:
                item["result"] = msg.get("result") or {}
                item["error"] = (msg.get("error") or {}).get("message") or ""
                item["event"].set()
            return
        self._events.append(msg)

    def feed(self, method, params=None):
        """扩展桥把 chrome.debugger 的事件喂进来。"""
        self._handle({"method": method, "params": params or {}})

    def send(self, method, params=None, timeout=20):
        if self._closed:
            raise CDPError("调试连接已断开")
        if self._rpc is not None:
            try:
                return self._rpc(method, params or {}, timeout) or {}
            except CDPError:
                raise
            except Exception as e:
                raise CDPError("%s 失败：%s" % (method, e))
        with self._lock:
            self._id += 1
            mid = self._id
            item = {"event": threading.Event(), "result": {}, "error": ""}
            self._pending[mid] = item
        payload = json.dumps({"id": mid, "method": method, "params": params or {}},
                             ensure_ascii=False)
        try:
            with self._send_lock:
                self._ws.send_text(payload)
        except OSError as e:
            raise CDPError("发送失败：%s" % e)
        if not item["event"].wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise CDPError("命令超时（%s）" % method)
        if item["error"]:
            raise CDPError("%s 失败：%s" % (method, item["error"]))
        return item["result"]

    # ---------------------------------------------------------- 事件
    def take_event(self, method, timeout=0.0):
        """取一条指定事件（取走即删除）。timeout>0 时轮询等待。"""
        deadline = time.time() + timeout
        while True:
            with self._lock:
                for i, ev in enumerate(self._events):
                    if ev.get("method") == method:
                        del self._events[i]
                        return ev.get("params") or {}
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def events(self, method, limit=30):
        """查看（不取走）最近的若干条事件。"""
        out = []
        with self._lock:
            for ev in reversed(self._events):
                if ev.get("method") == method:
                    out.append(ev.get("params") or {})
                    if len(out) >= limit:
                        break
        out.reverse()
        return out

    def clear_events(self):
        with self._lock:
            self._events.clear()

    def close(self):
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass
