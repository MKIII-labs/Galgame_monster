# -*- coding: utf-8 -*-
"""手机网关集成测试：真实 TLS + WebSocket + E2E 帧 + 白名单转发"""
import asyncio
import base64
import hashlib
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.mobile import audit, ca, crypto, devices, gateway, identity, pairing

TOKEN = "test-token"


class _Upstream(BaseHTTPRequestHandler):
    """假的「主服务」：只认 X-Auth-Token，回脱敏状态。"""

    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.headers.get("X-Auth-Token") != TOKEN:
            return self._json({"error": "unauthorized"}, 403)
        return self._json({"ok": True, "sessions": [], "echo": self.path})

    def do_POST(self):
        if self.headers.get("X-Auth-Token") != TOKEN:
            return self._json({"error": "unauthorized"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        return self._json({"ok": True, "path": self.path,
                           "body": json.loads(body.decode("utf-8") or "{}")})


@pytest.fixture
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "IDENTITY_FILE", str(tmp_path / "identity.json"))
    monkeypatch.setattr(devices, "DEVICES_FILE", str(tmp_path / "devices.json"))
    monkeypatch.setattr(audit, "AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(ca, "TLS_DIR", str(tmp_path / "tls"))
    monkeypatch.setattr(ca, "CA_CERT", str(tmp_path / "tls" / "ca.pem"))
    monkeypatch.setattr(ca, "CA_KEY", str(tmp_path / "tls" / "ca.key"))
    monkeypatch.setattr(ca, "SRV_CERT", str(tmp_path / "tls" / "server.pem"))
    monkeypatch.setattr(ca, "SRV_KEY", str(tmp_path / "tls" / "server.key"))
    identity._CACHE = None
    devices.CACHE = {"devices": {}}
    monkeypatch.setattr(pairing, "_TICKETS", {})
    monkeypatch.setattr(pairing, "_PENDING", {})
    yield gateway.stop()
    identity._CACHE = None


def _ssl_ctx():
    ctx = ssl.create_default_context(cafile=ca.CA_CERT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _b64(raw):
    return base64.b64encode(bytes(raw)).decode("ascii")


def _new_key():
    return ec.generate_private_key(ec.SECP256R1())


def _sign(key, data):
    return key.sign(bytes(data), ec.ECDSA(hashes.SHA256()))


def _spki(key):
    return _b64(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))


async def _connect(port, mode, device_key=None, device_id=""):
    import aiohttp
    session = aiohttp.ClientSession()
    ws = await session.ws_connect("wss://127.0.0.1:%d/ws" % port, ssl=_ssl_ctx())
    hello = json.loads((await ws.receive()).data)
    fp = hello["fp"]
    assert fp == identity.get_identity().fingerprint

    e1 = _new_key()
    cnonce = _b64(crypto.new_nonce(32))
    await ws.send_str(json.dumps({"t": "hello", "v": 1, "mode": mode,
                                  "device_id": device_id, "e1": _spki(e1),
                                  "cnonce": cnonce, "ticket_id": ""}))
    challenge = json.loads((await ws.receive()).data)
    fields = {"v": 1, "mode": mode, "sid": challenge["sid"], "device_id": device_id,
              "e1": _spki(e1), "e2": challenge["e2"], "cnonce": cnonce,
              "snonce": hello["snonce"], "fp": fp}
    transcript = crypto.transcript_bytes(fields)

    # 手机侧用二维码里的指纹校验桌面签名
    pub = serialization.load_der_public_key(base64.b64decode(hello["sign_pub"]))
    pub.verify(base64.b64decode(challenge["sig"]), transcript, ec.ECDSA(hashes.SHA256()))
    assert identity.fingerprint_of(hello["sign_pub"]) == fp
    return session, ws, hello, challenge, e1, transcript


async def _finish(ws, hello, challenge, e1, transcript, device_key):
    await ws.send_str(json.dumps({"t": "auth", "sig": _b64(_sign(device_key, transcript))}))
    ready = json.loads((await ws.receive()).data)
    peer_pub = serialization.load_der_public_key(base64.b64decode(challenge["e2"]))
    shared = e1.exchange(ec.ECDH(), peer_pub)
    c2s, s2c, c2s_pre, s2c_pre = crypto.derive_session_keys(shared, transcript)
    cipher = crypto.FrameCipher(send_key=c2s, recv_key=s2c,
                                send_prefix=c2s_pre, recv_prefix=s2c_pre)
    return ready, cipher


@pytest.mark.parametrize("path", ["/api/mobile/state", "/api/config"])
def test_gateway_whitelist_and_proxy(upstream, path):
    st = gateway.start(0, upstream, TOKEN)
    assert st["running"], st
    port = st["port"]
    dev_key = _new_key()
    dev = devices.add_device("Test", _spki(dev_key), days=1)

    async def scenario():
        session, ws, hello, challenge, e1, transcript = await _connect(
            port, "connect", dev_key, dev["id"])
        ready, cipher = await _finish(ws, hello, challenge, e1, transcript, dev_key)
        assert ready["t"] == "ready"
        body = json.dumps({"x": 1}).encode("utf-8")
        req = {"id": "r1", "method": "POST", "path": path,
               "body": _b64(body)}
        if path != "/api/config":
            req["ts"] = int(time.time())
            digest = hashlib.sha256(body).hexdigest()
            msg = ("%s\nPOST\n%s\n%d\n%s" % (dev["id"], path, req["ts"], digest)).encode()
            req["sig"] = _b64(_sign(dev_key, msg))
        await ws.send_bytes(cipher.seal(crypto.T_REQ, crypto.jpack(req)))
        frames = []
        while True:
            msg = await ws.receive(timeout=10)
            if msg.type != __import__("aiohttp").WSMsgType.BINARY:
                continue
            kind, payload = cipher.open(msg.data)
            frames.append((kind, crypto.junpack(payload)))
            if kind in (crypto.T_RES_END, crypto.T_ERROR):
                break
        await ws.close()
        await session.close()
        return frames

    frames = asyncio.run(scenario())
    kinds = [k for k, _ in frames]
    if path == "/api/config":
        assert kinds[0] == crypto.T_ERROR
        assert "白名单" in frames[0][1]["error"]
    else:
        assert kinds[0] == crypto.T_RES_HEAD
        assert frames[0][1]["status"] == 200
        assert crypto.T_RES_END in kinds


def test_gateway_rejects_unknown_device(upstream):
    st = gateway.start(0, upstream, TOKEN)
    port = st["port"]
    dev_key = _new_key()

    async def scenario():
        session, ws, hello, challenge, e1, transcript = await _connect(
            port, "connect", dev_key, "d_unknown")
        await ws.send_str(json.dumps({"t": "auth", "sig": _b64(_sign(dev_key, transcript))}))
        msg = json.loads((await ws.receive()).data)
        await ws.close()
        await session.close()
        return msg

    msg = asyncio.run(scenario())
    assert msg["t"] == "error"


def test_gateway_rejects_unsigned_sensitive_request(upstream):
    st = gateway.start(0, upstream, TOKEN)
    port = st["port"]
    dev_key = _new_key()
    dev = devices.add_device("Test", _spki(dev_key), days=1)

    async def scenario():
        session, ws, hello, challenge, e1, transcript = await _connect(
            port, "connect", dev_key, dev["id"])
        _ready, cipher = await _finish(ws, hello, challenge, e1, transcript, dev_key)
        req = {"id": "r1", "method": "POST", "path": "/api/chat",
               "body": _b64(b"{}")}
        await ws.send_bytes(cipher.seal(crypto.T_REQ, crypto.jpack(req)))
        msg = await ws.receive(timeout=10)
        kind, payload = cipher.open(msg.data)
        await ws.close()
        await session.close()
        return kind, crypto.junpack(payload)

    kind, payload = asyncio.run(scenario())
    assert kind == crypto.T_ERROR
    assert "签名" in payload["error"]


def test_pairing_flow_requires_desktop_confirm(upstream):
    st = gateway.start(0, upstream, TOKEN)
    port = st["port"]
    ticket = pairing.create_ticket()
    dev_key = _new_key()

    def _approve():
        for _ in range(200):
            items = pairing.list_pending()
            if items:
                pairing.resolve(items[0]["pair_id"], True)
                return
            threading.Event().wait(0.05)

    threading.Thread(target=_approve, daemon=True).start()

    async def scenario():
        import aiohttp
        session = aiohttp.ClientSession()
        ws = await session.ws_connect("wss://127.0.0.1:%d/ws" % port, ssl=_ssl_ctx())
        hello = json.loads((await ws.receive()).data)
        e1 = _new_key()
        cnonce = _b64(crypto.new_nonce(32))
        await ws.send_str(json.dumps({"t": "hello", "v": 1, "mode": "pair",
                                      "device_id": "", "e1": _spki(e1),
                                      "cnonce": cnonce,
                                      "ticket_id": ticket["pair_id"]}))
        challenge = json.loads((await ws.receive()).data)
        fields = {"v": 1, "mode": "pair", "sid": challenge["sid"], "device_id": "",
                  "e1": _spki(e1), "e2": challenge["e2"], "cnonce": cnonce,
                  "snonce": hello["snonce"], "fp": hello["fp"]}
        transcript = crypto.transcript_bytes(fields)
        proof = crypto.pair_proof(ticket["secret"], transcript)
        await ws.send_str(json.dumps({"t": "auth", "proof": _b64(proof),
                                      "name": "Pixel 8", "platform": "android",
                                      "sign_pub": _spki(dev_key)}))
        waiting = json.loads((await ws.receive()).data)
        assert waiting["t"] == "waiting" and len(waiting["sas"]) == 6
        paired = json.loads((await ws.receive()).data)
        await ws.close()
        await session.close()
        return waiting, paired

    waiting, paired = asyncio.run(scenario())
    assert paired["t"] == "paired"
    assert devices.get(paired["device_id"])["name"] == "Pixel 8"
    assert len(waiting["sas"]) == 6 and waiting["sas"].isdigit()
