# -*- coding: utf-8 -*-
"""手机远控 P0：身份 / 加密帧 / 配对票据 / 设备表 / 审计 / 本地 CA / 白名单"""
import base64
import time

import pytest

from app.mobile import audit, ca, crypto, devices, identity, pairing, policy


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
    monkeypatch.setattr(pairing, "_FAILS", [])
    yield
    identity._CACHE = None


# ---------------- 身份 ----------------

def test_identity_persists_and_signs(tmp_path):
    ident = identity.get_identity()
    assert ident.fingerprint.count("-") == 4
    assert len(ident.fingerprint.replace("-", "")) == 20
    sig = ident.sign(b"hello")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    pub = serialization.load_der_public_key(base64.b64decode(ident.sign_pub))
    pub.verify(sig, b"hello", ec.ECDSA(hashes.SHA256()))

    identity._CACHE = None                      # 模拟重启：从文件重新加载
    again = identity.get_identity()
    assert again.fingerprint == ident.fingerprint
    assert again.sign_pub == ident.sign_pub


def test_identity_reset_changes_fingerprint():
    first = identity.get_identity().fingerprint
    second = identity.reset_identity().fingerprint
    assert first != second


# ---------------- 加密帧 ----------------

def _pair_cipher():
    from cryptography.hazmat.primitives.asymmetric import ec
    a = ec.generate_private_key(ec.SECP256R1())
    b = ec.generate_private_key(ec.SECP256R1())
    transcript = crypto.transcript_bytes({"sid": "x", "e1": "a", "e2": "b"})
    shared_a = a.exchange(ec.ECDH(), b.public_key())
    shared_b = b.exchange(ec.ECDH(), a.public_key())
    assert shared_a == shared_b
    c2s, s2c, p1, p2 = crypto.derive_session_keys(shared_a, transcript)
    client = crypto.FrameCipher(send_key=c2s, recv_key=s2c,
                                send_prefix=p1, recv_prefix=p2)
    server = crypto.FrameCipher(send_key=s2c, recv_key=c2s,
                                send_prefix=p2, recv_prefix=p1)
    return client, server


def test_frame_roundtrip_and_replay_rejected():
    client, server = _pair_cipher()
    frame = client.seal(crypto.T_REQ, b'{"id":"1"}')
    kind, payload = server.open(frame)
    assert kind == crypto.T_REQ and payload == b'{"id":"1"}'
    with pytest.raises(crypto.CryptoError):
        server.open(frame)                      # 重放


def test_frame_tamper_rejected():
    client, server = _pair_cipher()
    frame = bytearray(client.seal(crypto.T_PING, b"x"))
    frame[-2] ^= 0x01
    with pytest.raises(crypto.CryptoError):
        server.open(bytes(frame))


def test_replay_window_tolerates_reorder():
    win = crypto.ReplayWindow()
    assert win.accept(5) and win.accept(6) and win.accept(4)
    assert not win.accept(4)
    assert win.accept(70)
    assert not win.accept(5)                    # 滑出窗口


def test_sas_is_stable_and_six_digits():
    t = crypto.transcript_bytes({"a": 1})
    assert crypto.sas_code(t) == crypto.sas_code(t)
    assert len(crypto.sas_code(t)) == 6 and crypto.sas_code(t).isdigit()


# ---------------- 配对 ----------------

def test_ticket_single_use_and_proof():
    ticket = pairing.create_ticket()
    transcript = crypto.transcript_bytes({"pair": ticket["pair_id"]})
    proof = crypto.pair_proof(ticket["secret"], transcript)
    ok, _why = pairing.verify_proof(ticket["pair_id"], transcript, proof)
    assert ok
    ok2, why2 = pairing.verify_proof(ticket["pair_id"], transcript, proof)
    assert not ok2 and "票据" in why2           # 单次使用


def test_ticket_expired(monkeypatch):
    ticket = pairing.create_ticket(ttl=1)
    monkeypatch.setattr(time, "time", lambda: ticket["expires"] + 5)
    transcript = crypto.transcript_bytes({"pair": ticket["pair_id"]})
    ok, why = pairing.verify_proof(ticket["pair_id"], transcript,
                                   crypto.pair_proof(ticket["secret"], transcript))
    assert not ok and "过期" in why


def test_ticket_wrong_proof_and_attempt_limit():
    ticket = pairing.create_ticket()
    transcript = crypto.transcript_bytes({"pair": ticket["pair_id"]})
    for _ in range(pairing.MAX_ATTEMPTS):
        ok, _why = pairing.verify_proof(ticket["pair_id"], transcript, b"bad")
        assert not ok
    ok, why = pairing.verify_proof(ticket["pair_id"], transcript,
                                   crypto.pair_proof(ticket["secret"], transcript))
    assert not ok and "次数" in why


def test_pairing_cooldown_after_failures():
    for _ in range(pairing.FAIL_LIMIT):
        pairing._FAILS.append(time.time())
    with pytest.raises(pairing.PairingError):
        pairing.create_ticket()


def test_pending_decision_roundtrip():
    pairing.add_pending({"pair_id": "p1", "name": "Pixel", "sas": "123456",
                         "created": time.time()})
    assert pairing.list_pending()[0]["sas"] == "123456"
    assert pairing.resolve("p1", True)
    assert pairing.wait_decision("p1", 1) is True
    assert pairing.list_pending() == []


def test_qr_payload_has_no_long_term_secret():
    ticket = pairing.create_ticket()
    url = pairing.pair_url("https://192.168.1.5:57890", "ABCD-EFGH-IJKL-MNOP-QRST",
                           ticket, "Pixel 8")
    assert url.startswith("https://192.168.1.5:57890/#v=1&fp=")
    assert "t=" + ticket["pair_id"] in url
    assert ticket["secret"] in url               # 一次性票据允许进二维码
    assert "device" not in url and "token" not in url


# ---------------- 设备表 ----------------

def _device():
    ident = identity.get_identity()
    return devices.add_device("Pixel 8", ident.sign_pub, platform="android", days=30)


def test_device_lifecycle_and_signature():
    dev = _device()
    assert devices.is_active(dev)
    assert devices.get(dev["id"])["name"] == "Pixel 8"

    ident = identity.get_identity()
    msg = b"transcript"
    assert devices.verify_signature(dev, msg, ident.sign(msg))
    assert not devices.verify_signature(dev, b"other", ident.sign(msg))

    assert devices.revoke(dev["id"])
    assert not devices.is_active(devices.get(dev["id"]))


def test_device_expiry(monkeypatch):
    dev = _device()
    monkeypatch.setattr(time, "time", lambda: dev["expires"] + 1)
    assert not devices.is_active(devices.get(dev["id"]))


# ---------------- 审计 ----------------

def test_audit_redacts_secrets():
    audit.record("pair_ok", name="Pixel", ticket="secret-value", nested={"a": 1})
    item = audit.tail(5)[0]
    assert item["action"] == "pair_ok"
    assert item["ticket"] == "[redacted]"
    assert item["name"] == "Pixel"


# ---------------- 本地 CA ----------------

def test_ca_certificates_cover_lan_ip():
    certs = ca.ensure_server_cert(["192.168.1.5", "10.0.0.8"])
    assert certs[0].endswith("server.pem")
    ctx = ca.ssl_context(["192.168.1.5"])
    assert ctx.minimum_version >= ca.ssl.TLSVersion.TLSv1_2
    assert len(ca.ca_fingerprint().split(":")) == 32
    pem = ca.ca_pem()
    assert b"BEGIN CERTIFICATE" in pem


# ---------------- 白名单 ----------------

def test_firewall_rule_is_scoped_to_local_subnet():
    from app.mobile import lan
    cmd = lan._rule_command(57890)
    assert "localsubnet" in cmd
    assert "private,public" in cmd          # 家用 Wi-Fi 常被判成「公用」
    assert "57890" in cmd


def test_policy_whitelist():
    assert policy.allowed("POST", "/api/chat")
    assert policy.allowed("POST", "/api/agent/approve")
    assert policy.allowed("GET", "/api/mobile/state")
    assert not policy.allowed("POST", "/api/config")
    assert not policy.allowed("GET", "/api/state")
    assert not policy.allowed("POST", "/api/terminal/run")
    assert policy.is_sensitive("/api/agent/approve")
    assert not policy.is_sensitive("/api/session/messages")
