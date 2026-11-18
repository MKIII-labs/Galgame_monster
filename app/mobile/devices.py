# -*- coding: utf-8 -*-
"""手机设备表：登记公钥 / 权限、最后在线、吊销与过期。"""
import base64
import secrets
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.store import read_json, write_json
from app.mobile import DEVICES_FILE, DEFAULT_SCOPES, ensure_dir

_LOCK = threading.RLock()
CACHE = {"devices": {}}


def _load():
    with _LOCK:
        if CACHE.get("_loaded"):
            return CACHE["devices"]
        data = read_json(DEVICES_FILE)
        devices = data.get("devices") if isinstance(data, dict) else None
        CACHE["devices"] = devices if isinstance(devices, dict) else {}
        CACHE["_loaded"] = True
        return CACHE["devices"]


def _save():
    ensure_dir()
    write_json(DEVICES_FILE, {"v": 1, "devices": CACHE["devices"]})


def add_device(name, sign_pub, platform="", days=180, scopes=None):
    sign_pub = str(sign_pub or "").strip()
    if not sign_pub:
        raise ValueError("设备公钥为空")
    now = time.time()
    device = {
        "id": "d_" + secrets.token_hex(8),
        "name": str(name or "手机")[:64],
        "platform": str(platform or "")[:32],
        "sign_pub": sign_pub,
        "created": now,
        "last_seen": now,
        "expires": now + max(1, int(days)) * 86400,
        "scopes": list(scopes or DEFAULT_SCOPES),
        "revoked": False,
    }
    with _LOCK:
        _load()
        CACHE["devices"][device["id"]] = device
        _save()
    return dict(device)


def get(device_id):
    with _LOCK:
        item = _load().get(str(device_id or ""))
        return dict(item) if item else None


def list_all():
    with _LOCK:
        items = [dict(v) for v in _load().values()]
    return sorted(items, key=lambda d: float(d.get("created") or 0), reverse=True)


def touch(device_id):
    with _LOCK:
        item = _load().get(str(device_id or ""))
        if not item:
            return False
        item["last_seen"] = time.time()
        _save()
        return True


def revoke(device_id):
    with _LOCK:
        item = _load().get(str(device_id or ""))
        if not item:
            return False
        item["revoked"] = True
        _save()
        return True


def revoke_all():
    with _LOCK:
        for item in _load().values():
            item["revoked"] = True
        _save()


def is_active(device):
    if not device or device.get("revoked"):
        return False
    try:
        return float(device.get("expires") or 0) > time.time()
    except (TypeError, ValueError):
        return False


def verify_signature(device, data, signature):
    """用设备公钥验证 ECDSA(SHA-256) 签名。"""
    try:
        der = base64.b64decode(str((device or {}).get("sign_pub") or ""))
        pub = serialization.load_der_public_key(der)
        pub.verify(bytes(signature), bytes(data), ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:                                            # noqa: BLE001
        return False


def reset():
    with _LOCK:
        CACHE["devices"] = {}
        CACHE["_loaded"] = True
        _save()
