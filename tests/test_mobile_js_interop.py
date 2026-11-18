# -*- coding: utf-8 -*-
"""手机端 JS 加解密与 Python 后端的互通自检（需要 node；没有就跳过）。

覆盖：transcript 规范化、ECDH+HKDF 派生、帧格式、SAS 短码。
"""
import base64
import json
import os
import shutil
import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.mobile import crypto

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


def _b64(raw):
    return base64.b64encode(bytes(raw)).decode("ascii")


@pytest.mark.skipif(not NODE, reason="没有 node，跳过 JS 互通自检")
def test_js_crypto_interop(tmp_path):
    e1 = ec.generate_private_key(ec.SECP256R1())
    e2 = ec.generate_private_key(ec.SECP256R1())
    fields = {"v": 1, "mode": "connect", "sid": "sid-1", "device_id": "d_1",
              "e1": "E1PUB", "e2": "E2PUB", "cnonce": "C" * 44,
              "snonce": "S" * 44, "fp": "AAAA-BBBB-CCCC-DDDD-EEEE"}
    transcript = crypto.transcript_bytes(fields)
    shared = e1.exchange(ec.ECDH(), e2.public_key())
    c2s, s2c, p1, p2 = crypto.derive_session_keys(shared, transcript)
    cipher = crypto.FrameCipher(send_key=c2s, recv_key=s2c,
                                send_prefix=p1, recv_prefix=p2)
    peer = crypto.FrameCipher(send_key=s2c, recv_key=c2s,
                              send_prefix=p2, recv_prefix=p1)
    payloads = [(crypto.T_REQ, b'{"id":"r1","method":"POST"}'),
                (crypto.T_PING, b''),
                (crypto.T_RES_CHUNK, b'{"id":"r1","data":"aGk="}')]
    frames = [{"type": t, "payload": _b64(p), "frame_hex": cipher.seal(t, p).hex()}
              for t, p in payloads]
    recv_frames = [{"type": t, "payload": _b64(p), "frame_hex": peer.seal(t, p).hex()}
                   for t, p in payloads]

    fixture = {
        "fields": fields,
        "transcript_hex": transcript.hex(),
        "e1_pkcs8": _b64(e1.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())),
        "e2_spki": _b64(e2.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)),
        "c2s": _b64(c2s), "s2c": _b64(s2c),
        "c2s_prefix": _b64(p1), "s2c_prefix": _b64(p2),
        "frames": frames,
        "recv_frames": recv_frames,
        "sas": crypto.sas_code(transcript),
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    script = os.path.join(ROOT, "tests", "js", "interop.mjs")
    app_js = os.path.join(ROOT, "web", "m", "app.js")
    proc = subprocess.run([NODE, script, app_js, str(path)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout
