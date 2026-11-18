# -*- coding: utf-8 -*-
"""手机远控（第一期：同一 Wi-Fi 局域网）。

设计见 docs/MOBILE_REMOTE_DESIGN.md。本包只依赖标准库与 cryptography / qrcode，
不导入主服务内部对象：网关通过本机 HTTP + 主 TOKEN 调用白名单 API。
"""
import os

from app.store import DATA_DIR

MOBILE_DIR = os.path.join(DATA_DIR, "mobile")
IDENTITY_FILE = os.path.join(MOBILE_DIR, "identity.json")
DEVICES_FILE = os.path.join(MOBILE_DIR, "devices.json")
AUDIT_FILE = os.path.join(MOBILE_DIR, "audit.jsonl")
TLS_DIR = os.path.join(MOBILE_DIR, "tls")

PROTOCOL_VERSION = 1
DEFAULT_PORT = 57890
# 手机端只能访问这些能力（其余路径网关直接拒绝，不发给主服务）
DEFAULT_SCOPES = ("chat", "approve", "shot", "voice")


def ensure_dir():
    os.makedirs(MOBILE_DIR, exist_ok=True)
    return MOBILE_DIR
