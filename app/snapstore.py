# -*- coding: utf-8 -*-
"""撤销快照的磁盘持久化。

`agent.py` 里的 `_UNDO` 是权威内存结构，这里只负责把它落到
`data/undo/<session>.json`，让进程重启后还能接着撤销 / 重做。

取舍：
- **单文件超过 1MB 的快照不落盘**（图片 / 大二进制改动只在本次进程内可回滚）；
- **单会话总量超过 8MB 时整体放弃持久化**，不影响内存撤销；
- 读失败 / 版本不符一律当空，绝不因为缓存坏了让对话报错。
"""
import base64
import hashlib
import json
import os

from app.store import DATA_DIR

UNDO_DIR = os.path.join(DATA_DIR, "undo")
MAX_FILE = 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024
VERSION = 1


def _path(sid):
    key = hashlib.sha1(str(sid or "").encode("utf-8", "ignore")).hexdigest()[:16]
    return os.path.join(UNDO_DIR, key + ".json")


def _enc_op(op):
    """bytes 转 base64；超过单文件上限返回 None（该条不落盘）。"""
    d = dict(op)
    for key, flag in (("data", "_b64"), ("after", "_after_b64")):
        val = d.get(key)
        if isinstance(val, (bytes, bytearray)):
            if len(val) > MAX_FILE:
                return None
            d[key] = base64.b64encode(bytes(val)).decode("ascii")
            d[flag] = True
    return d


def _dec_op(op):
    d = dict(op)
    if d.pop("_b64", False):
        try:
            d["data"] = base64.b64decode(d.get("data") or "")
        except (ValueError, TypeError):
            d["data"] = b""
    if d.pop("_after_b64", False):
        try:
            d["after"] = base64.b64decode(d.get("after") or "")
        except (ValueError, TypeError):
            d["after"] = b""
    return d


def save(sid, items):
    """items: {token: {"label", "ts", "undone", "ops"}}"""
    if not sid:
        return
    try:
        os.makedirs(UNDO_DIR, exist_ok=True)
        out, total = {}, 0
        for token, item in (items or {}).items():
            ops, ok = [], True
            for op in item.get("ops") or []:
                enc = _enc_op(op)
                if enc is None:
                    ok = False
                    break
                ops.append(enc)
            if not ok:
                continue
            entry = {"label": item.get("label") or "",
                     "ts": item.get("ts") or 0,
                     "undone": bool(item.get("undone")),
                     "ops": ops}
            total += len(json.dumps(entry, ensure_ascii=False))
            if total > MAX_TOTAL:
                break
            out[token] = entry
        path = _path(sid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "tokens": out}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        pass


def load(sid):
    if not sid:
        return {}
    try:
        with open(_path(sid), "r", encoding="utf-8") as f:
            data = json.load(f)
        if int(data.get("version") or 0) != VERSION:
            return {}
        out = {}
        for token, item in (data.get("tokens") or {}).items():
            out[token] = {"label": item.get("label") or "",
                          "ts": item.get("ts") or 0,
                          "undone": bool(item.get("undone")),
                          "ops": [_dec_op(o) for o in (item.get("ops") or [])]}
        return out
    except (OSError, ValueError, TypeError):
        return {}


def clear(sid):
    try:
        os.remove(_path(sid))
    except OSError:
        pass
