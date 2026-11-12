# -*- coding: utf-8 -*-
"""查询级检索缓存（P1）。

缓存的是「服务商原始结果」，不含排序分：同一 provider + 检索式 + 条数在 TTL 内
直接复用，排序仍按本轮 intent / freshness 重新算。时效类查询 TTL 自动缩短，
避免把「昨天的股价」缓存一整天。
"""
import hashlib
import json
import os
import threading
import time

from app.store import DATA_DIR

CACHE_FILE = os.path.join(DATA_DIR, "cache", "search_cache.json")
MAX_ENTRIES = 600
_LOCK = threading.Lock()


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


def _key(provider, url, query, count):
    raw = "%s|%s|%s|%s" % (provider, url, str(query or "").strip().lower(), count)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def ttl_for(intent="general", freshness="", default=3600):
    """时效类缩短，文档类放长；default 来自设置里的 search_cache_ttl。"""
    try:
        base = max(60, int(default or 3600))
    except (TypeError, ValueError):
        base = 3600
    if freshness in ("hour", "day") or intent in ("fresh", "news"):
        return min(base, 15 * 60)
    if intent in ("docs", "academic", "howto"):
        return min(max(base, 60 * 60), 12 * 3600)
    return min(base, 6 * 3600)


def get(provider, url, query, count, enabled=True):
    """命中返回 items（list）；未命中 / 过期 / 关闭返回 None。"""
    if not enabled:
        return None
    k = _key(provider, url, query, count)
    now = time.time()
    with _LOCK:
        data = _load()
        rec = data.get(k)
        if not isinstance(rec, dict):
            return None
        try:
            ts = float(rec.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        ttl = float(rec.get("ttl") or 0)
        if not ts or not ttl or now - ts > ttl:
            data.pop(k, None)
            _save(data)
            return None
        items = rec.get("items")
        return items if isinstance(items, list) else None


def put(provider, url, query, count, items, ttl=3600):
    if not items:
        return
    k = _key(provider, url, query, count)
    now = time.time()
    with _LOCK:
        data = _load()
        # 清过期 + 控制条目数
        for key in list(data.keys()):
            rec = data.get(key)
            if not isinstance(rec, dict):
                data.pop(key, None)
                continue
            try:
                if now - float(rec.get("ts") or 0) > float(rec.get("ttl") or 0):
                    data.pop(key, None)
            except (TypeError, ValueError):
                data.pop(key, None)
        data[k] = {"ts": now, "ttl": max(60, int(ttl or 3600)), "items": items[:30]}
        if len(data) > MAX_ENTRIES:
            keep = sorted(data.items(), key=lambda kv: float(kv[1].get("ts") or 0),
                          reverse=True)[:MAX_ENTRIES]
            data = dict(keep)
        _save(data)


def clear():
    with _LOCK:
        _save({})
