# -*- coding: utf-8 -*-
"""审计日志：追加写 JSONL，5MB 滚动一份；按字段名脱敏，不记正文与密钥。"""
import json
import os
import threading
import time

from app.mobile import AUDIT_FILE, ensure_dir

_LOCK = threading.Lock()
MAX_BYTES = 5 * 1024 * 1024
MAX_STR = 300
_SECRET_KEYS = ("secret", "ticket", "token", "password", "private", "proof",
                "signature", "api_key", "auth")


def _clean(key, value):
    k = str(key).lower()
    if any(s in k for s in _SECRET_KEYS):
        return "[redacted]"
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[:MAX_STR] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_STR]


def record(action, **fields):
    """追加一条审计。失败静默（审计不能反过来阻断业务）。"""
    item = {"ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": str(action)[:64]}
    for k, v in fields.items():
        item[str(k)] = _clean(k, v)
    line = json.dumps(item, ensure_ascii=False)
    try:
        ensure_dir()
        with _LOCK:
            try:
                if os.path.getsize(AUDIT_FILE) > MAX_BYTES:
                    os.replace(AUDIT_FILE, AUDIT_FILE + ".1")
            except OSError:
                pass
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass
    return item


def tail(limit=200):
    """读取最近 limit 条（新的在前）。"""
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for raw in lines[-max(1, int(limit)):]:
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    out.reverse()
    return out
