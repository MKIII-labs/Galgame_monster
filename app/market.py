# -*- coding: utf-8 -*-
"""插件市场：DSH 生态只读索引 + 官方插件市场索引。

DSH：数据来自 awesome-dsh-plugin.com 的公开 JSON，服务端分页 + 缓存。
这些是 Cordis/TypeScript 插件，跑不进本项目运行时，所以这里只展示、不安装。

官方市场：索引里的条目带 https 下载地址 + sha256，安装走 plugin_manager.install_url。
索引地址由 config.plugin_index_url 指定；留空时读随包的 resources/plugins/market.json。

（原先的「本地 Skill 管理」已移除：Skill 的增删启停统一在 设置 → Skill 里做。）
"""
import json
import os
import re
import threading
import time

import requests

from app.config import load_config
from app.store import DATA_DIR, RESOURCES_DIR, read_json, write_json

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


# ------------------------------------------------------------
# 官方插件市场（可安装）：索引 → 缓存 → 前端卡片
# ------------------------------------------------------------
PLUGIN_INDEX_FILE = os.path.join(RESOURCES_DIR, "plugins", "market.json")
PLUGIN_INDEX_CACHE = os.path.join(DATA_DIR, "cache", "plugin_index.json")
PLUGIN_INDEX_TTL = 3600
PLUGIN_INDEX_MAX_BYTES = 2 * 1024 * 1024

_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_INDEX_LOCK = threading.Lock()


def _clean_plugin(it):
    """索引条目 → 前端字段。id / https 地址 / sha256 不合法就丢弃对应字段。"""
    if not isinstance(it, dict):
        return None
    pid = str(it.get("id") or "").strip()
    if not _PLUGIN_ID_RE.match(pid):
        return None
    url = str(it.get("url") or "").strip()
    if url and not url.lower().startswith("https://"):
        url = ""
    sha = str(it.get("sha256") or "").strip().lower()
    if not _SHA256_RE.match(sha):
        sha = ""
    try:
        size = max(0, int(it.get("size") or 0))
    except (TypeError, ValueError):
        size = 0
    return {
        "id": pid,
        "name": str(it.get("name") or pid),
        "version": str(it.get("version") or ""),
        "author": str(it.get("author") or ""),
        "description": str(it.get("description") or ""),
        "permissions": [str(x) for x in (it.get("permissions") or [])][:12],
        "homepage": str(it.get("homepage") or ""),
        "url": url,
        "sha256": sha,
        "size": size,
    }


def _validate_index(data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("索引格式不对（需要 items 数组）")
    items, seen = [], set()
    for it in data["items"]:
        row = _clean_plugin(it)
        if row and row["id"] not in seen:
            seen.add(row["id"])
            items.append(row)
    return {"items": items, "updated": str(data.get("updated") or "")}


def _fetch_plugin_index(url):
    if not url.lower().startswith("https://"):
        raise ValueError("索引地址必须是 https://")
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    if len(r.content or b"") > PLUGIN_INDEX_MAX_BYTES:
        raise ValueError("索引文件过大")
    return r.json()


def plugin_index(force=False):
    """返回市场索引 {items, updated, stale, source, error}。

    config.plugin_index_url 为空 → 直接读随包索引（本地文件很小，不做缓存）；
    否则按 TTL 缓存远端索引，拉取失败时回退旧缓存并标记 stale；
    从未成功拉取过才抛 RuntimeError。
    """
    cfg = load_config()
    url = str(cfg.get("plugin_index_url") or "").strip()
    if not url:
        try:
            with open(PLUGIN_INDEX_FILE, "r", encoding="utf-8") as f:
                data = _validate_index(json.load(f))
        except (OSError, ValueError) as e:
            return {"items": [], "updated": "", "stale": False, "source": "local",
                    "error": "读取随包索引失败：%s" % e}
        return {**data, "stale": False, "source": "local", "error": ""}

    try:
        ttl = max(0, int(cfg.get("plugin_index_ttl", PLUGIN_INDEX_TTL)))
    except (TypeError, ValueError):
        ttl = PLUGIN_INDEX_TTL
    now = time.time()

    def cached():
        c = read_json(PLUGIN_INDEX_CACHE, default=None)
        if isinstance(c, dict) and c.get("url") == url and isinstance(c.get("data"), dict):
            return c
        return None

    def ok(data, stale=False, error=""):
        return {**data, "stale": stale, "source": "remote", "error": error}

    if not force:
        c = cached()
        if c and now - float(c.get("at") or 0) < ttl:
            return ok(c["data"])
    with _INDEX_LOCK:
        if not force:
            c = cached()
            if c and now - float(c.get("at") or 0) < ttl:
                return ok(c["data"])
        try:
            data = _validate_index(_fetch_plugin_index(url))
            os.makedirs(os.path.dirname(PLUGIN_INDEX_CACHE), exist_ok=True)
            write_json(PLUGIN_INDEX_CACHE, {"at": now, "url": url, "data": data})
            return ok(data)
        except Exception as e:                      # noqa: BLE001  网络/格式都可能失败
            c = cached()
            if c:
                return ok(c["data"], stale=True, error=str(e))
            raise RuntimeError("拉取插件市场索引失败：%s" % e)
