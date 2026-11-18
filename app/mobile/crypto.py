# -*- coding: utf-8 -*-
"""握手与帧加解密：HKDF-SHA256 派生 + AES-256-GCM，带严格单调计数与重放窗口。

帧格式（与 web/m/app.js 的 JS 实现必须保持一致）：
    magic(2)="GM" | version(1) | type(1) | flags(1) | prefix(4) | counter(8) | ciphertext
AAD 就是前面这 17 字节的头部。
"""
import hashlib
import hmac
import json
import os
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"GM"
VERSION = 1
HEADER_LEN = 17

T_REQ = 1
T_RES_HEAD = 2
T_RES_CHUNK = 3
T_RES_END = 4
T_CANCEL = 5
T_PING = 6
T_PONG = 7
T_ERROR = 8

HKDF_INFO = b"gm-mobile-v1"


class CryptoError(Exception):
    pass


def _hkdf(secret, salt, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


def derive_session_keys(shared, transcript):
    """返回 (c2s_key, s2c_key, c2s_prefix, s2c_prefix)。"""
    salt = hashlib.sha256(transcript).digest()
    raw = _hkdf(shared, salt, HKDF_INFO, 72)
    return raw[:32], raw[32:64], raw[64:68], raw[68:72]


def transcript_bytes(fields):
    """规范化 transcript：键排序的紧凑 JSON（两侧字节一致）。"""
    return json.dumps(fields, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sas_code(transcript):
    """6 位短码：由 transcript 派生，两侧必须一致。"""
    digest = hashlib.sha256(bytes(transcript) + b"|sas").digest()
    return "%06d" % (int.from_bytes(digest[:4], "big") % 1000000)


def pair_proof(ticket_secret, transcript):
    """配对证明：HMAC-SHA256(ticket_secret, transcript)。"""
    return hmac.new(str(ticket_secret).encode("utf-8"), bytes(transcript),
                    hashlib.sha256).digest()


def verify_pair_proof(ticket_secret, transcript, proof):
    try:
        return hmac.compare_digest(pair_proof(ticket_secret, transcript), bytes(proof))
    except (TypeError, ValueError):
        return False


class ReplayWindow:
    """64 位滑窗：只接受没见过的更大计数（乱序容忍 64 帧）。"""

    def __init__(self, size=64):
        self.size = int(size)
        self.highest = -1
        self._bits = 0

    def accept(self, counter):
        if counter < 0:
            return False
        if counter > self.highest:
            shift = counter - self.highest if self.highest >= 0 else self.size
            if self.highest < 0 or shift >= self.size:
                self._bits = 1
            else:
                self._bits = ((self._bits << shift) | 1) & ((1 << self.size) - 1)
            self.highest = counter
            return True
        delta = self.highest - counter
        if delta >= self.size:
            return False
        bit = 1 << delta
        if self._bits & bit:
            return False
        self._bits |= bit
        return True


class FrameCipher:
    """一个连接上的双向帧加解密器。"""

    MAX_COUNTER = (1 << 63) - 1

    def __init__(self, send_key, recv_key, send_prefix, recv_prefix):
        self._send = AESGCM(bytes(send_key))
        self._recv = AESGCM(bytes(recv_key))
        self._send_prefix = bytes(send_prefix)
        self._recv_prefix = bytes(recv_prefix)
        self._counter = 0
        self._window = ReplayWindow()

    def seal(self, kind, payload=b""):
        if self._counter > self.MAX_COUNTER:
            raise CryptoError("counter exhausted")
        header = (MAGIC + bytes([VERSION, int(kind) & 0xFF, 0]) + self._send_prefix
                  + struct.pack(">Q", self._counter))
        nonce = header[5:9] + header[9:17]
        ct = self._send.encrypt(nonce, bytes(payload or b""), header)
        self._counter += 1
        return header + ct

    def open(self, frame):
        data = bytes(frame or b"")
        if len(data) < HEADER_LEN + 16:
            raise CryptoError("frame too short")
        if data[:2] != MAGIC or data[2] != VERSION:
            raise CryptoError("bad magic/version")
        kind = data[3]
        prefix = data[5:9]
        counter = struct.unpack(">Q", data[9:17])[0]
        if prefix != self._recv_prefix:
            raise CryptoError("bad nonce prefix")
        if not self._window.accept(counter):
            raise CryptoError("replay or out of window")
        nonce = prefix + struct.pack(">Q", counter)
        try:
            return kind, self._recv.decrypt(nonce, data[HEADER_LEN:], data[:HEADER_LEN])
        except Exception as exc:                     # noqa: BLE001  统一转成协议错误
            raise CryptoError("decrypt failed: %s" % exc)


def new_nonce(size=32):
    return os.urandom(size)


def jpack(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def junpack(raw):
    try:
        return json.loads(bytes(raw).decode("utf-8"))
    except Exception:
        return {}
