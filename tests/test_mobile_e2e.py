# -*- coding: utf-8 -*-
"""手机访问端到端：真实主服务 + 网关 + 配对 + 白名单转发（不涉及模型请求）"""
import asyncio
import base64
import json
import ssl
import threading
import time
import urllib.request

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import aiohttp

from app.mobile import audit, ca, crypto, devices, gateway, identity, lan, pairing


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
    monkeypatch.setattr(lan, "ensure_firewall", lambda port: {"ok": True, "message": ""})
    monkeypatch.setattr(lan, "firewall_status",
                        lambda: {"supported": True, "present": True})
    identity._CACHE = None
    devices.CACHE = {"devices": {}}
    monkeypatch.setattr(pairing, "_TICKETS", {})
    monkeypatch.setattr(pairing, "_PENDING", {})
    monkeypatch.setattr(pairing, "_FAILS", [])
    yield gateway.stop()
    identity._CACHE = None


def _post(port, path, body, token):
    raw = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=raw,
                                 headers={"X-Auth-Token": token,
                                          "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ssl_ctx():
    ctx = ssl.create_default_context(cafile=ca.CA_CERT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _b64(raw):
    return base64.b64encode(bytes(raw)).decode("ascii")


def _spki(key):
    return _b64(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))


@pytest.fixture
def server(tmp_path, monkeypatch):
    from app import httpd, sessions
    from app.config import load_config, save_config
    monkeypatch.setattr(sessions, "SESSION_FILE", str(tmp_path / "sessions.json"))
    cfg = load_config()
    cfg["mobile_enabled"] = False
    cfg["mobile_port"] = 0
    save_config(cfg)
    srv, port = httpd.create_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, port
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:
        pass


def test_full_pair_and_proxy(server):
    from app import httpd
    srv, port = server
    # 开启手机访问（端口 0 = 临时端口）
    st = _post(port, "/api/mobile/enable", {}, httpd.TOKEN)
    assert st["ok"], st
    gw = st["gateway"]
    assert gw["running"], gw
    gport = gw["port"]

    # 生成配对二维码载荷
    pair = _post(port, "/api/mobile/pair/start", {}, httpd.TOKEN)
    assert pair["ok"] and "<svg" in pair["qr_svg"]
    frag = pair["payload"].split("#", 1)[1]
    params = dict(kv.split("=", 1) for kv in frag.split("&"))
    ticket_id, secret = params["t"].split(".", 1)
    assert params["fp"] == pair["fingerprint"]

    dev_key = ec.generate_private_key(ec.SECP256R1())

    def _confirm():
        for _ in range(300):
            pending = _post(port, "/api/mobile/pair/pending", {}, httpd.TOKEN)["pending"]
            if pending:
                _post(port, "/api/mobile/pair/confirm",
                      {"pair_id": pending[0]["pair_id"], "ok": True}, httpd.TOKEN)
                return
            time.sleep(0.05)
        raise AssertionError("没有收到待确认配对")

    threading.Thread(target=_confirm, daemon=True).start()

    async def scenario():
        session = aiohttp.ClientSession()
        ws = await session.ws_connect("wss://127.0.0.1:%d/ws" % gport, ssl=_ssl_ctx())
        hello = json.loads((await ws.receive()).data)
        e1 = ec.generate_private_key(ec.SECP256R1())
        cnonce = _b64(crypto.new_nonce(32))
        await ws.send_str(json.dumps({"t": "hello", "v": 1, "mode": "pair",
                                      "device_id": "", "e1": _spki(e1),
                                      "cnonce": cnonce, "ticket_id": ticket_id}))
        challenge = json.loads((await ws.receive()).data)
        fields = {"v": 1, "mode": "pair", "sid": challenge["sid"], "device_id": "",
                  "e1": _spki(e1), "e2": challenge["e2"], "cnonce": cnonce,
                  "snonce": hello["snonce"], "fp": hello["fp"]}
        transcript = crypto.transcript_bytes(fields)
        proof = crypto.pair_proof(secret, transcript)
        await ws.send_str(json.dumps({"t": "auth", "proof": _b64(proof),
                                      "name": "Pixel 8", "platform": "android",
                                      "sign_pub": _spki(dev_key)}))
        waiting = json.loads((await ws.receive()).data)
        assert waiting["t"] == "waiting"
        paired = json.loads((await ws.receive()).data)
        assert paired["t"] == "paired", paired
        device_id = paired["device_id"]

        # 用配对得到的会话密钥发一条请求：拿脱敏状态
        peer = serialization.load_der_public_key(base64.b64decode(challenge["e2"]))
        shared = e1.exchange(ec.ECDH(), peer)
        c2s, s2c, p1, p2 = crypto.derive_session_keys(shared, transcript)
        cipher = crypto.FrameCipher(send_key=c2s, recv_key=s2c,
                                    send_prefix=p1, recv_prefix=p2)
        body = b"{}"
        req = {"id": "r1", "method": "POST", "path": "/api/mobile/state", "body": _b64(body)}
        await ws.send_bytes(cipher.seal(crypto.T_REQ, crypto.jpack(req)))
        chunks, status = [], 0
        while True:
            msg = await ws.receive(timeout=15)
            if msg.type != aiohttp.WSMsgType.BINARY:
                continue
            kind, payload = cipher.open(msg.data)
            if kind == crypto.T_RES_HEAD:
                status = crypto.junpack(payload)["status"]
            elif kind == crypto.T_RES_CHUNK:
                chunks.append(base64.b64decode(crypto.junpack(payload)["data"]))
            elif kind == crypto.T_RES_END:
                break
            elif kind == crypto.T_ERROR:
                raise AssertionError(crypto.junpack(payload))
        await ws.close()
        await session.close()
        return status, b"".join(chunks), device_id

    status, raw, device_id = asyncio.run(scenario())
    assert status == 200
    data = json.loads(raw.decode("utf-8"))
    assert data["ok"] is True
    assert "config" not in data and "sessions" in data
    assert devices.get(device_id)["name"] == "Pixel 8"

    # 关闭后端口不再可用
    st2 = _post(port, "/api/mobile/disable", {}, httpd.TOKEN)
    assert not st2["gateway"]["running"]


def _get(port, path):
    ctx = _ssl_ctx()
    with urllib.request.urlopen("https://127.0.0.1:%d%s" % (port, path),
                                context=ctx, timeout=15) as resp:
        return resp.status, dict(resp.headers), resp.read()


def test_gateway_serves_shell_and_rejects_unknown_paths(server):
    from app import httpd
    _srv, port = server
    st = _post(port, "/api/mobile/enable", {}, httpd.TOKEN)
    gport = st["gateway"]["port"]
    assert gport

    code, headers, body = _get(gport, "/")
    assert code == 200 and b"Galgame Monster" in body
    assert "default-src 'self'" in headers.get("Content-Security-Policy", "")
    assert headers.get("X-Frame-Options") == "DENY"

    code, headers, body = _get(gport, "/app.js")
    assert code == 200 and b"E2E" in body
    assert "javascript" in headers.get("Content-Type", "")

    code, _h, body = _get(gport, "/ca.crt")
    assert code == 200 and b"BEGIN CERTIFICATE" in body

    code, _h, body = _get(gport, "/handshake.json")
    info = json.loads(body.decode("utf-8"))
    from app.mobile import identity as ident_mod
    assert info["fingerprint"] == ident_mod.get_identity().fingerprint
    assert info["ca_fingerprint"]

    for bad in ("/app/config.py", "/../app/config.py", "/static/app.js", "/index"):
        try:
            code, _h, _b = _get(gport, bad)
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 404, bad

    _post(port, "/api/mobile/disable", {}, httpd.TOKEN)
