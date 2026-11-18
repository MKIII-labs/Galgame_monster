# -*- coding: utf-8 -*-
"""桌面身份密钥：ECDSA P-256（签名）+ ECDH P-256（协商），落盘时用 DPAPI 加密。"""
import base64
import hashlib
import os
import threading
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.store import read_json, write_json
from app.mobile import IDENTITY_FILE, ensure_dir
from app.mobile import dpapi

_LOCK = threading.RLock()
_CACHE = None


def _pub_b64(key):
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode("ascii")


def fingerprint_of(pub_b64):
    """公钥指纹：SHA-256(SPKI DER) 的 Base32 前 20 字符，4 字符一组。"""
    try:
        der = base64.b64decode(str(pub_b64 or ""))
    except Exception:
        return ""
    b32 = base64.b32encode(hashlib.sha256(der).digest()).decode("ascii").rstrip("=")
    body = b32[:20]
    return "-".join(body[i:i + 4] for i in range(0, 20, 4))


class Identity:
    def __init__(self, sign_key, dh_key, created=0.0):
        self.sign_key = sign_key
        self.dh_key = dh_key
        self.created = float(created or time.time())

    @property
    def sign_pub(self):
        return _pub_b64(self.sign_key)

    @property
    def dh_pub(self):
        return _pub_b64(self.dh_key)

    @property
    def fingerprint(self):
        return fingerprint_of(self.sign_pub)

    def sign(self, data):
        return self.sign_key.sign(data, ec.ECDSA(hashes.SHA256()))

    def public_info(self):
        return {"v": 1, "fingerprint": self.fingerprint,
                "sign_pub": self.sign_pub, "dh_pub": self.dh_pub,
                "created": self.created}


def _load_key(sealed):
    pem = dpapi.open_text(sealed)
    if not pem:
        return None
    try:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception:
        return None


def _read_file():
    data = read_json(IDENTITY_FILE)
    if not isinstance(data, dict):
        return None
    sign_key = _load_key(data.get("sign_key"))
    dh_key = _load_key(data.get("dh_key"))
    if sign_key is None or dh_key is None:
        return None
    return Identity(sign_key, dh_key, data.get("created") or 0)


def _write_file(ident):
    ensure_dir()
    pem = lambda k: k.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    write_json(IDENTITY_FILE, {
        "v": 1,
        "created": ident.created,
        "sign_key": dpapi.seal_text(pem(ident.sign_key)),
        "dh_key": dpapi.seal_text(pem(ident.dh_key)),
    })


def get_identity(refresh=False):
    """读取（或首次生成）桌面身份；进程内缓存。"""
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not refresh:
            return _CACHE
        ident = None if refresh else _read_file()
        if ident is None:
            ident = Identity(ec.generate_private_key(ec.SECP256R1()),
                             ec.generate_private_key(ec.SECP256R1()),
                             time.time())
            _write_file(ident)
        _CACHE = ident
        return ident


def reset_identity():
    """重置身份：调用方必须同时吊销全部设备（旧设备将无法再连接）。"""
    global _CACHE
    with _LOCK:
        try:
            os.remove(IDENTITY_FILE)
        except OSError:
            pass
        _CACHE = None
    return get_identity()
