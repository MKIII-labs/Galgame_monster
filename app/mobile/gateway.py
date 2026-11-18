# -*- coding: utf-8 -*-
"""局域网移动网关：HTTPS（本地 CA）+ WebSocket + 应用层 E2E + 白名单转发。

主服务继续只监听 127.0.0.1；网关在独立线程里跑自己的事件循环，只暴露
一个端口，且只服务 web/m 的静态壳与移动白名单 API。
"""
import asyncio
import base64
import hashlib
import json
import os
import socket
import threading
import time

from aiohttp import web

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.mobile import PROTOCOL_VERSION, crypto, devices, identity, lan, pairing, policy, audit, ca
from app.store import WEB_DIR

M_DIR = os.path.join(WEB_DIR, "m")

# 只允许这些文件经网关返回（避免把任意路径暴露给局域网）
STATIC_FILES = {
    "": "index.html",
    "index.html": "index.html",
    "app.js": "app.js",
    "style.css": "style.css",
    "manifest.webmanifest": "manifest.webmanifest",
    "sw.js": "sw.js",
    "icons/icon-192.png": "icons/icon-192.png",
    "icons/icon-512.png": "icons/icon-512.png",
}

_LOCK = threading.RLock()
_STATE = {
    "thread": None, "loop": None, "runner": None, "site": None,
    "port": 0, "main_port": 0, "token": "", "error": "", "started": 0.0,
    "conns": 0, "url": "",
}
_READY = threading.Event()
# AppKey 必须在模块帧里创建：aiohttp 会向上找 co_name == "<module>" 的栈帧，
# 在网关线程里创建会抛 UnboundLocalError。
_SESSION_KEY = web.AppKey("mobile_session", object)


def _b64(raw):
    return base64.b64encode(bytes(raw)).decode("ascii")


def _load_pub(text):
    return serialization.load_der_public_key(base64.b64decode(str(text or "")))


def _pub_b64(key):
    return _b64(key.public_bytes(serialization.Encoding.DER,
                                 serialization.PublicFormat.SubjectPublicKeyInfo))


def _json(raw):
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _device_days():
    try:
        from app.config import load_config
        return int(load_config().get("mobile_device_days") or 180)
    except Exception:
        return 180


def status():
    with _LOCK:
        running = bool(_STATE["thread"] and _STATE["thread"].is_alive()
                       and _STATE["site"] is not None)
        port = int(_STATE["port"] or 0)
        return {
            "running": running,
            "port": port,
            "main_port": int(_STATE["main_port"] or 0),
            # 地址实时按当前网卡算：换了 Wi-Fi 不用重启也知道新地址
            "url": ("https://%s:%d" % (lan.lan_ip() or "127.0.0.1", port)) if running else "",
            "lan_ip": lan.lan_ip(),
            "error": _STATE["error"],
            "connections": int(_STATE["conns"] or 0),
            "started": _STATE["started"],
        }


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ---------------- 静态壳 ----------------

async def _file(name, cache="no-store"):
    from aiohttp import web
    full = os.path.join(M_DIR, STATIC_FILES[name])
    ctype = None
    for ext, mime in ((".html", "text/html; charset=utf-8"),
                      (".js", "application/javascript; charset=utf-8"),
                      (".css", "text/css; charset=utf-8"),
                      (".webmanifest", "application/manifest+json; charset=utf-8"),
                      (".png", "image/png")):
        if full.endswith(ext):
            ctype = mime
            break
    resp = web.FileResponse(full, headers={"Content-Type": ctype} if ctype else None)
    resp.headers["Cache-Control"] = cache
    return resp


async def _index(request):
    return await _file("index.html")


async def _static(request):
    from aiohttp import web
    name = request.match_info.get("name") or ""
    if name not in STATIC_FILES:
        raise web.HTTPNotFound()
    cache = "no-cache" if name in ("sw.js", "manifest.webmanifest") else "no-store"
    return await _file(name, cache)


async def _ca_cert(request):
    from aiohttp import web
    return web.Response(body=ca.ca_pem(), content_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="gm-mobile-ca.crt"'})


async def _handshake_info(request):
    from aiohttp import web
    ident = identity.get_identity()
    return web.json_response({"v": PROTOCOL_VERSION, **ident.public_info(),
                              "ca_fingerprint": _safe(ca.ca_fingerprint, ""),
                              "lan_ip": lan.lan_ip()})


# ---------------- 应用 ----------------

def _build_app(main_port, token):
    async def ws_handler(request):
        return await _ws_session(request, main_port, token)

    @web.middleware
    async def headers_mw(request, handler):
        resp = await handler(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; "
            "font-src 'self'; base-uri 'none'; form-action 'none'")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    app = web.Application(middlewares=[headers_mw])
    app[_SESSION_KEY] = None
    app.router.add_get("/", _index)
    app.router.add_get("/ca.crt", _ca_cert)
    app.router.add_get("/handshake.json", _handshake_info)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/{name:.*}", _static)

    async def _startup(app):
        from aiohttp import ClientSession
        app[_SESSION_KEY] = ClientSession(timeout=None, trust_env=False)
        _CURRENT_SESSION.set(app[_SESSION_KEY])

    async def _cleanup(app):
        if app[_SESSION_KEY] is not None:
            await app[_SESSION_KEY].close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app


# ---------------- WebSocket 会话 ----------------

async def _ws_session(request, main_port, token):
    from aiohttp import web, WSMsgType
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
    await ws.prepare(request)
    with _LOCK:
        _STATE["conns"] = int(_STATE["conns"] or 0) + 1
    peer = request.remote or ""
    try:
        ident = identity.get_identity()
        snonce = crypto.new_nonce(32)
        await ws.send_str(json.dumps({
            "t": "hello", "v": PROTOCOL_VERSION, "fp": ident.fingerprint,
            "sign_pub": ident.sign_pub, "dh_pub": ident.dh_pub,
            "snonce": _b64(snonce),
        }))

        first = await _recv(ws, 15)
        if first is None:
            return ws
        hello = _json(first)
        if str(hello.get("t")) != "hello" or int(hello.get("v") or 0) != PROTOCOL_VERSION:
            await _reject(ws, "协议版本不匹配")
            return ws
        mode = str(hello.get("mode") or "")
        if mode not in ("pair", "connect"):
            await _reject(ws, "未知的握手模式")
            return ws

        e1 = str(hello.get("e1") or "")
        cnonce = str(hello.get("cnonce") or "")
        device_id = str(hello.get("device_id") or "")
        try:
            e1_pub = _load_pub(e1)
            if len(base64.b64decode(cnonce)) < 16:
                raise ValueError("nonce")
            if not isinstance(e1_pub, ec.EllipticCurvePublicKey):
                raise ValueError("curve")
        except Exception:
            await _reject(ws, "公钥参数不正确")
            return ws

        sid = _b64(crypto.new_nonce(12))
        e2_key = ec.generate_private_key(ec.SECP256R1())
        e2_pub = _pub_b64(e2_key.public_key())
        fields = {"v": PROTOCOL_VERSION, "mode": mode, "sid": sid,
                  "device_id": device_id, "e1": e1, "e2": e2_pub,
                  "cnonce": cnonce, "snonce": _b64(snonce),
                  "fp": ident.fingerprint}
        transcript = crypto.transcript_bytes(fields)
        await ws.send_str(json.dumps({"t": "challenge", "sid": sid, "e2": e2_pub,
                                      "sig": _b64(ident.sign(transcript)),
                                      "fp": ident.fingerprint}))

        second = await _recv(ws, 15)
        if second is None:
            return ws
        auth = _json(second)
        if str(auth.get("t")) != "auth":
            await _reject(ws, "握手未完成")
            return ws

        device = None
        if mode == "pair":
            ticket_id = str(hello.get("ticket_id") or "")
            try:
                proof = base64.b64decode(str(auth.get("proof") or ""))
            except Exception:
                proof = b""
            ok, why = pairing.verify_proof(ticket_id, transcript, proof)
            if not ok:
                audit.record("pair_failed", ip=peer, reason=why)
                await _reject(ws, "配对校验失败：" + why)
                return ws
            name = str(auth.get("name") or "手机")[:64]
            platform = str(auth.get("platform") or "")[:32]
            sign_pub = str(auth.get("sign_pub") or "")
            if not sign_pub:
                await _reject(ws, "缺少设备公钥")
                return ws
            sas = crypto.sas_code(transcript)
            pairing.add_pending({"pair_id": ticket_id, "name": name, "sas": sas,
                                 "created": time.time(), "sign_pub": sign_pub,
                                 "platform": platform})
            audit.record("pair_request", ip=peer, name=name, sas=sas)
            try:
                from app import events
                events.publish({"type": "mobile_pair", "pair_id": ticket_id,
                                "name": name, "sas": sas})
            except Exception:
                pass
            await ws.send_str(json.dumps({"t": "waiting", "sas": sas}))
            decision = await asyncio.get_running_loop().run_in_executor(
                None, pairing.wait_decision, ticket_id, 120.0)
            if not decision:
                audit.record("pair_denied", ip=peer, name=name)
                await ws.send_str(json.dumps({"t": "denied", "error": "桌面未确认配对"}))
                await ws.close()
                return ws
            device = devices.add_device(name, sign_pub, platform=platform,
                                        days=_device_days())
            audit.record("pair_ok", ip=peer, name=name, device=device["id"])
            await ws.send_str(json.dumps({"t": "paired", "device_id": device["id"],
                                          "name": device["name"]}))
        else:
            device = devices.get(device_id)
            if not devices.is_active(device):
                audit.record("connect_rejected", ip=peer, device=device_id, reason="not active")
                await _reject(ws, "设备未授权或已过期")
                return ws
            try:
                sig = base64.b64decode(str(auth.get("sig") or ""))
            except Exception:
                sig = b""
            if not devices.verify_signature(device, transcript, sig):
                audit.record("connect_rejected", ip=peer, device=device_id,
                             reason="bad signature")
                await _reject(ws, "设备签名校验失败")
                return ws
            devices.touch(device_id)
            audit.record("connect", ip=peer, device=device_id)
            await ws.send_str(json.dumps({"t": "ready", "device_id": device_id}))

        shared = e2_key.exchange(ec.ECDH(), e1_pub)
        c2s, s2c, c2s_pre, s2c_pre = crypto.derive_session_keys(shared, transcript)
        cipher = crypto.FrameCipher(send_key=s2c, recv_key=c2s,
                                    send_prefix=s2c_pre, recv_prefix=c2s_pre)
        await _frame_loop(ws, cipher, device, main_port, token, peer)
        return ws
    finally:
        with _LOCK:
            _STATE["conns"] = max(0, int(_STATE["conns"] or 0) - 1)


async def _recv(ws, timeout):
    from aiohttp import WSMsgType
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    except asyncio.TimeoutError:
        await ws.close()
        return None
    if msg.type != WSMsgType.TEXT:
        await ws.close()
        return None
    return msg.data


async def _reject(ws, message):
    try:
        await ws.send_str(json.dumps({"t": "error", "error": message}))
        await ws.close()
    except Exception:
        pass


async def _frame_loop(ws, cipher, device, main_port, token, peer):
    from aiohttp import WSMsgType
    send_q = asyncio.Queue(maxsize=256)
    tasks = {}

    async def writer():
        while True:
            data = await send_q.get()
            if data is None:
                return
            try:
                await ws.send_bytes(data)
            except Exception:
                return

    def send(kind, obj):
        try:
            send_q.put_nowait(cipher.seal(kind, crypto.jpack(obj)))
        except asyncio.QueueFull:
            pass

    writer_task = asyncio.create_task(writer())
    try:
        while True:
            try:
                msg = await ws.receive(timeout=90)
            except asyncio.TimeoutError:
                audit.record("idle_timeout", ip=peer)
                break
            if msg.type == WSMsgType.BINARY:
                try:
                    kind, payload = cipher.open(msg.data)
                except crypto.CryptoError as exc:
                    audit.record("frame_rejected", ip=peer, reason=str(exc))
                    break
                if kind == crypto.T_PING:
                    send(crypto.T_PONG, {})
                elif kind == crypto.T_CANCEL:
                    req = crypto.junpack(payload)
                    task = tasks.pop(str(req.get("id") or ""), None)
                    if task:
                        task.cancel()
                elif kind == crypto.T_REQ:
                    req = crypto.junpack(payload)
                    rid = str(req.get("id") or "")
                    if not rid or rid in tasks:
                        continue
                    if len(tasks) >= 4:
                        send(crypto.T_ERROR, {"id": rid, "error": "并发请求过多"})
                        continue
                    task = asyncio.create_task(
                        _proxy(req, device, main_port, token, send, peer))
                    tasks[rid] = task
                    task.add_done_callback(lambda _t, k=rid: tasks.pop(k, None))
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
    finally:
        for task in list(tasks.values()):
            task.cancel()
        try:
            send_q.put_nowait(None)
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(writer_task, timeout=3)
        except Exception:
            pass


def _req_signature_ok(device, req, body):
    """敏感请求必须由设备私钥签名：device_id + method + path + ts + body hash。"""
    if not device:
        return False
    try:
        ts = float(req.get("ts") or 0)
        if abs(time.time() - ts) > 120:
            return False
        sig = base64.b64decode(str(req.get("sig") or ""))
    except Exception:
        return False
    digest = hashlib.sha256(body or b"").hexdigest()
    msg = ("%s\n%s\n%s\n%d\n%s" % (device.get("id"), str(req.get("method")).upper(),
                                   str(req.get("path")), int(ts), digest)).encode("utf-8")
    return devices.verify_signature(device, msg, sig)


async def _proxy(req, device, main_port, token, send, peer):
    rid = str(req.get("id") or "")
    method = str(req.get("method") or "POST").upper()
    path = str(req.get("path") or "")
    if not policy.allowed(method, path):
        audit.record("api_denied", ip=peer, device=(device or {}).get("id"), path=path)
        send(crypto.T_ERROR, {"id": rid, "error": "该接口不在移动端白名单内"})
        return
    try:
        body = base64.b64decode(str(req.get("body") or ""))
    except Exception:
        send(crypto.T_ERROR, {"id": rid, "error": "请求体编码错误"})
        return
    if len(body) > policy.MAX_BODY:
        send(crypto.T_ERROR, {"id": rid, "error": "请求体过大"})
        return
    if policy.is_sensitive(path) and not _req_signature_ok(device, req, body):
        audit.record("api_denied", ip=peer, device=(device or {}).get("id"),
                     path=path, reason="unsigned")
        send(crypto.T_ERROR, {"id": rid, "error": "敏感操作需要设备签名"})
        return

    session = _CURRENT_SESSION.get()
    if session is None:
        send(crypto.T_ERROR, {"id": rid, "error": "网关未就绪"})
        return
    url = "http://127.0.0.1:%d%s" % (int(main_port), path)
    headers = {"X-Auth-Token": token}
    # 只放行一个自定义头：语音识别需要采样率（其余头一律忽略）
    extra = req.get("headers") if isinstance(req.get("headers"), dict) else {}
    sample_rate = str(extra.get("X-Sample-Rate") or "").strip()
    if sample_rate.isdigit():
        headers["X-Sample-Rate"] = sample_rate
    if body:
        headers["Content-Type"] = ("application/octet-stream" if path == "/api/stt"
                                   else "application/json")
    try:
        async with session.request(method, url, data=body or None, headers=headers) as resp:
            send(crypto.T_RES_HEAD, {"id": rid, "status": resp.status,
                                     "ctype": resp.headers.get("Content-Type", "")})
            async for chunk in resp.content.iter_any():
                if chunk:
                    send(crypto.T_RES_CHUNK, {"id": rid, "data": _b64(chunk)})
            send(crypto.T_RES_END, {"id": rid})
    except asyncio.CancelledError:
        send(crypto.T_ERROR, {"id": rid, "error": "已取消"})
        raise
    except Exception as exc:                     # noqa: BLE001
        send(crypto.T_ERROR, {"id": rid, "error": "转发失败：%s" % exc})


class _CurrentSession:
    """每个线程（事件循环）自己的 ClientSession。"""

    def __init__(self):
        self._local = threading.local()

    def set(self, session):
        self._local.session = session

    def get(self):
        return getattr(self._local, "session", None)


_CURRENT_SESSION = _CurrentSession()


# ---------------- 启停 ----------------

def _port_in_use(port):
    """Windows 允许 SO_REUSEADDR 重复绑定同一端口，会静默抢不到流量；先探一下。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start(port, main_port, token):
    """启动网关；返回 status()。已在运行则直接返回。"""
    with _LOCK:
        if (_STATE["thread"] and _STATE["thread"].is_alive()
                and _STATE["site"] is not None):
            return status()
        if port and _port_in_use(port):
            _STATE.update({"error": "端口 %d 已被占用，请在设置里换一个端口" % int(port),
                           "site": None, "runner": None, "port": 0, "url": "",
                           "thread": None, "conns": 0})
            return status()
        ips = lan.candidate_ips()
        ctx = ca.ssl_context(ips)
        _STATE.update({"port": int(port), "main_port": int(main_port), "token": str(token),
                       "error": "", "site": None, "runner": None, "started": time.time()})
        _READY.clear()
        thread = threading.Thread(target=_run, args=(int(port), int(main_port), str(token), ctx),
                                  name="mobile-gateway", daemon=True)
        _STATE["thread"] = thread
        thread.start()
    _READY.wait(15)
    return status()


def _run(port, main_port, token, ctx):
    from aiohttp import web
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _LOCK:
        _STATE["loop"] = loop
    try:
        app = _build_app(main_port, token)
        runner = web.AppRunner(app, access_log=None)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "0.0.0.0", port, ssl_context=ctx, reuse_address=True)
        loop.run_until_complete(site.start())
        actual = site._server.sockets[0].getsockname()[1]      # noqa: SLF001
        with _LOCK:
            _STATE.update({"runner": runner, "site": site, "port": int(actual),
                           "url": "https://%s:%d" % (lan.lan_ip() or "127.0.0.1", actual),
                           "started": time.time()})
        _READY.set()
        loop.run_forever()
    except Exception as exc:                     # noqa: BLE001
        import traceback
        traceback.print_exc()
        with _LOCK:
            _STATE.update({"error": "网关启动失败：%s" % exc, "site": None,
                           "runner": None, "port": 0, "url": ""})
        _READY.set()
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        with _LOCK:
            _STATE.update({"site": None, "runner": None, "loop": None, "conns": 0})


def stop():
    with _LOCK:
        loop = _STATE.get("loop")
        runner = _STATE.get("runner")
        thread = _STATE.get("thread")
    if loop is None or thread is None:
        with _LOCK:
            _STATE.update({"site": None, "runner": None, "port": 0, "url": ""})
        return status()

    async def _shutdown():
        try:
            if runner is not None:
                await runner.cleanup()
        except Exception:
            pass

    try:
        asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=10)
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    thread.join(timeout=6)
    with _LOCK:
        _STATE.update({"site": None, "runner": None, "loop": None, "port": 0,
                       "url": "", "thread": None, "conns": 0})
    return status()
