# -*- coding: utf-8 -*-
"""网页正文缓存：同一 URL + 检索式 + 抓取档位在短 TTL 内复用。

搜索列表本身已有 `search_cache`；这里缓存的是「抓回来的正文 / 证据片段」，
避免跨轮搜索、补搜和研究子代理重复抓同一页面。
"""
import hashlib
import json
import os
import threading
import time

from app.store import DATA_DIR

CACHE_FILE = os.path.join(DATA_DIR, "cache", "page_cache.json")
MAX_ENTRIES = 300
DEFAULT_TTL = 30 * 60
_LOCK = threading.Lock()
_MEM = None


def _load():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:                      # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception:                      # noqa: BLE001  缓存失败不能影响搜索
        pass


def _mem():
    global _MEM
    if _MEM is None:
        _MEM = _load()
    return _MEM


def reset():
    """测试用：丢掉进程内缓存。"""
    global _MEM
    _MEM = None


def key(url, reader, query, chars):
    raw = "%s|%s|%s|%s" % (reader or "", url or "",
                           " ".join(str(query or "").split()).lower(), chars or 0)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def get(key):
    """命中返回 (text, evidence)；未命中 / 过期返回 None。"""
    now = time.time()
    with _LOCK:
        data = _mem()
        rec = data.get(key)
        if not isinstance(rec, dict):
            return None
        try:
            ts = float(rec.get("ts") or 0)
            ttl = float(rec.get("ttl") or 0)
        except (TypeError, ValueError):
            return None
        if not ts or not ttl or now - ts > ttl:
            data.pop(key, None)
            _save(data)
            return None
        text = rec.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        ev = rec.get("evidence")
        evidence = [dict(x) for x in ev if isinstance(x, dict)] if isinstance(ev, list) else []
        return text, evidence


def put(key, text, evidence, ttl=DEFAULT_TTL):
    if not text:
        return
    now = time.time()
    with _LOCK:
        data = _mem()
        ev = [dict(x) for x in (evidence or []) if isinstance(x, dict)][:8]
        data[key] = {"ts": now, "ttl": max(60, int(ttl or DEFAULT_TTL)),
                     "text": text, "evidence": ev}
        for k in list(data.keys()):
            rec = data.get(k)
            if not isinstance(rec, dict):
                data.pop(k, None)
                continue
            try:
                if now - float(rec.get("ts") or 0) > float(rec.get("ttl") or 0):
                    data.pop(k, None)
            except (TypeError, ValueError):
                data.pop(k, None)
        if len(data) > MAX_ENTRIES:
            keep = sorted(data.items(), key=lambda kv: float(kv[1].get("ts") or 0),
                          reverse=True)[:MAX_ENTRIES]
            data.clear()
            data.update(dict(keep))
        _save(data)


def clear():
    with _LOCK:
        data = _mem()
        data.clear()
        _save(data)
