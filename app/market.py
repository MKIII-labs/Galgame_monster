# -*- coding: utf-8 -*-
"""插件市场：DSH 生态只读索引。

数据来自 awesome-dsh-plugin.com 的公开 JSON，服务端分页 + 缓存。
这些是 Cordis/TypeScript 插件，跑不进本项目运行时，所以这里只展示、不安装。

（原先的「本地 Skill 管理」已移除：Skill 的增删启停统一在 设置 → Skill 里做。）
"""
import os
import threading
import time

import requests

from app.store import DATA_DIR, read_json, write_json

CACHE_FILE = os.path.join(DATA_DIR, "cache", "dsh_plugins.json")

DSH_INDEX_URL = "https://awesome-dsh-plugin.com/plugins.json"
DSH_TTL = 6 * 3600          # 6 小时内直接用缓存
DSH_MAX_PAGE = 60
DSH_MAX_BYTES = 32 * 1024 * 1024   # 防止误拉超大响应

_DSH_LOCK = threading.Lock()


def _clean_item(p):
    """DSH 索引条目 → 前端需要的字段（不整包透传，避免一次返回几 MB）。"""
    if not isinstance(p, dict):
        return None
    desc = p.get("description")
    if isinstance(desc, dict):
        desc = {"zh": str(desc.get("zh") or ""), "en": str(desc.get("en") or "")}
    elif desc is None:
        desc = {"zh": "", "en": ""}
    else:
        desc = {"zh": str(desc), "en": ""}
    caps = p.get("capabilities") if isinstance(p.get("capabilities"), list) else []
    return {
        "name": str(p.get("name") or ""),
        "owner": str(p.get("owner") or ""),
        "url": str(p.get("url") or ""),
        "page": str(p.get("page") or ""),
        "category": str(p.get("category") or ""),
        "description": desc,
        "npm": p.get("npm"),
        "version": p.get("version"),
        "stars": p.get("stars"),
        "downloads": p.get("downloads"),
        "capabilities": [str(c) for c in caps][:8],
        "install": str(p.get("install") or ""),
        "added": str(p.get("added") or ""),
    }


def _category_labels(raw):
    """索引里的分类是 {en, zh} 对象；前端要的是可显示的一行文字。"""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                out[str(k)] = str(v.get("zh") or v.get("en") or k)
            else:
                out[str(k)] = str(v or k)
    return out


def _fetch_dsh():
    r = requests.get(DSH_INDEX_URL, timeout=20, stream=False)
    r.raise_for_status()
    if len(r.content or b"") > DSH_MAX_BYTES:
        raise ValueError("索引文件过大")
    data = r.json()
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise ValueError("返回格式不是插件索引")
    return data


def _load_cache():
    cached = read_json(CACHE_FILE, default=None)
    if isinstance(cached, dict) and isinstance(cached.get("data"), dict):
        return cached
    return None


def dsh_index(force=False):
    """返回 (data, meta)。没有缓存且拉取失败时抛 RuntimeError。"""
    now = time.time()
    if not force:
        cached = _load_cache()
        if cached and now - float(cached.get("at") or 0) < DSH_TTL:
            return cached["data"], {"stale": False, "cached_at": cached.get("at") or 0}
    with _DSH_LOCK:
        # 二次检查：并发请求里只让一个真的去拉
        if not force:
            cached = _load_cache()
            if cached and now - float(cached.get("at") or 0) < DSH_TTL:
                return cached["data"], {"stale": False, "cached_at": cached.get("at") or 0}
        try:
            data = _fetch_dsh()
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            write_json(CACHE_FILE, {"at": now, "data": data})
            return data, {"stale": False, "cached_at": now}
        except Exception as e:                          # noqa: BLE001  网络/格式都可能失败
            cached = _load_cache()
            if cached:
                return cached["data"], {"stale": True, "error": str(e),
                                        "cached_at": cached.get("at") or 0}
            raise RuntimeError("拉取 DSH 插件索引失败：%s" % e)


def dsh_page(q="", category="", page=1, size=24, force=False):
    """DSH 索引的搜索 / 筛选 / 分页。"""
    data, meta = dsh_index(force)
    items = data.get("plugins") or []
    query = str(q or "").strip().lower()
    cat = str(category or "").strip()

    def hit(p):
        if cat and str(p.get("category") or "") != cat:
            return False
        if not query:
            return True
        desc = p.get("description") or {}
        hay = " ".join([
            str(p.get("name") or ""), str(p.get("owner") or ""),
            str(p.get("npm") or ""),
            str(desc.get("zh") or "") if isinstance(desc, dict) else str(desc),
            str(desc.get("en") or "") if isinstance(desc, dict) else "",
        ]).lower()
        return query in hay

    rows = [p for p in items if isinstance(p, dict) and hit(p)]
    try:
        size = max(6, min(DSH_MAX_PAGE, int(size)))
    except (TypeError, ValueError):
        size = 24
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    start = (page - 1) * size
    chunk = [_clean_item(p) for p in rows[start:start + size]]
    return {
        "items": [x for x in chunk if x],
        "total": len(rows),
        "page": page,
        "size": size,
        "categories": _category_labels(data.get("categories")),
        "updated": str(data.get("updated") or ""),
        "count": int(data.get("count") or len(items)),
        "stale": bool(meta.get("stale")),
        "error": meta.get("error") or "",
        "cached_at": meta.get("cached_at") or 0,
    }
