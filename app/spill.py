# -*- coding: utf-8 -*-
"""工具结果落盘（spill）：超长结果存文件，只回灌头尾 + 一个跨轮有效的 locator。

为什么需要：读整个文件 / 跑测试的输出可能几万到几十万字。全塞进上下文会让后续每一轮都
重传它；只给头尾又会让模型为了看细节重新读一遍。落盘后模型可以按字符区间取回，
既省上下文，又不用重跑工具。

与请求内句柄（`r1` / `r2`）的区别：那些只活一次请求，这里落盘的在重启后仍然有效。
"""
import json
import os
import re
import threading
import time
import uuid

from app.store import DATA_DIR

_LOCK = threading.Lock()
_LOC_RE = re.compile(r"^sp_[0-9a-f]{10}$")

DEFAULT_MAX = 50000          # 超过这个字符数才落盘
DEFAULT_KEEP = 400           # 最多保留多少个 spill 文件
DEFAULT_MB = 256             # 落盘总量上限（MB）


def _dir(data_dir=None):
    d = os.path.join(data_dir or DATA_DIR, "spills")
    os.makedirs(d, exist_ok=True)
    return d


def _paths(locator, data_dir=None):
    d = _dir(data_dir)
    return os.path.join(d, locator + ".txt"), os.path.join(d, locator + ".json")


def valid(locator):
    return bool(_LOC_RE.match(str(locator or "").strip()))


def save(session_id, tool, text, data_dir=None, keep=DEFAULT_KEEP, max_mb=DEFAULT_MB):
    """把一份工具结果落盘，返回 locator（形如 sp_1a2b3c4d5e）。

    写失败时返回空串，调用方应当退回到「头尾截断」路径。
    """
    try:
        body = str(text or "")
        loc = "sp_" + uuid.uuid4().hex[:10]
        p, m = _paths(loc, data_dir)
        with _LOCK:
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
            with open(m, "w", encoding="utf-8") as f:
                json.dump({"tool": str(tool or ""), "chars": len(body),
                           "session": str(session_id or ""), "ts": time.time()},
                          f, ensure_ascii=False)
        _gc(data_dir, keep, max_mb)
        return loc
    except Exception as e:
        print("[spill] 落盘失败（退回头尾截断）：%s" % e)
        return ""


def load(locator, data_dir=None):
    """按 locator 取回全文；不存在 / 非法返回 None。"""
    loc = str(locator or "").strip()
    if not valid(loc):
        return None
    try:
        p, _m = _paths(loc, data_dir)
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def meta(locator, data_dir=None):
    loc = str(locator or "").strip()
    if not valid(loc):
        return None
    try:
        _p, m = _paths(loc, data_dir)
        with open(m, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _gc(data_dir, keep, max_mb):
    """按最旧优先清理：文件数超过 keep 或总量超过 max_mb 时删掉最老的。"""
    if keep <= 0 and max_mb <= 0:
        return
    d = _dir(data_dir)
    entries, total = [], 0
    try:
        names = [n for n in os.listdir(d) if n.endswith(".txt")]
    except OSError:
        return
    for n in names:
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
        total += st.st_size
    entries.sort()
    while entries and ((keep > 0 and len(entries) > keep)
                       or (max_mb > 0 and total > max_mb * 1024 * 1024)):
        _mt, size, p = entries.pop(0)
        for x in (p, p[:-4] + ".json"):
            try:
                os.remove(x)
            except OSError:
                pass
        total -= size
