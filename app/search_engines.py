# -*- coding: utf-8 -*-
"""免注册搜索 / 抓页引擎链（移植自 liustack/modsearch 的引擎层）。

三个 keyless 引擎按序尝试，失败自动换下一个：
- firecrawl  /v2/search + /v2/scrape（云端跑浏览器，能读 JS 渲染页面）
- keenable   /v1/search/public（每小时 1000 次 / IP）
- youcom     /v1/agents/search（每天 100 次 / IP）

配额 / 鉴权失败会把该引擎冷却一段时间（data/cache/search_engines.json），
冷却期内直接跳过，不再浪费一次请求。

这里只做「请求 + 字段收敛」。modsearch 在 dsh 里的插件层（registerSearchProvider）
是别的运行时的接口，本项目入口是 app/search.py 的 search_web / _fetch_one。
"""
import os
import time
from urllib.parse import urlparse

import requests

from app.config import load_config
from app.store import DATA_DIR, read_json, write_json

STATE_FILE = os.path.join(DATA_DIR, "cache", "search_engines.json")

FIRECRAWL_BASE = "https://api.firecrawl.dev"
KEENABLE_BASE = "https://api.keenable.ai"
YOUCOM_URL = "https://api.you.com/v1/agents/search"

CHAIN = ("firecrawl", "keenable", "youcom")
ENV_KEYS = {"firecrawl": "FIRECRAWL_API_KEY",
            "keenable": "KEENABLE_API_KEY",
            "youcom": "YOUCOM_API_KEY"}
SNIPPET_MAX = 600          # Firecrawl 的 description 是整页 markdown，必须截断
DEFAULT_TIMEOUT = 20
SCRAPE_TIMEOUT = 30
QUOTA_COOLDOWN = 3600      # 配额 / 鉴权失败后冷却多久（秒）
QUALITY_MIN = 0.18         # 引擎结果与查询的最低相关度；低于它先换源，但会留作兜底


class EngineError(Exception):
    """kind: quota（额度）、auth（鉴权）、network（超时 / 5xx / 其它）。"""

    def __init__(self, message, kind="network", ttl=QUOTA_COOLDOWN):
        super().__init__(message)
        self.kind = kind
        self.ttl = ttl


# ---------------------------------------------------------------- 配置
def engine_keys(cfg=None):
    """每个引擎的 Key：设置页优先，其次环境变量（与 modsearch 一致）。"""
    cfg = cfg or load_config()
    raw = cfg.get("search_engine_keys")
    raw = raw if isinstance(raw, dict) else {}
    return {name: str(raw.get(name) or os.environ.get(env) or "").strip()
            for name, env in ENV_KEYS.items()}


def engine_enabled(cfg=None):
    """引擎是否参与自动链；缺省 True。"""
    cfg = cfg or load_config()
    raw = cfg.get("search_engine_enabled")
    raw = raw if isinstance(raw, dict) else {}
    return {name: bool(raw.get(name, True)) for name in CHAIN}


def modsearch_enabled(cfg=None):
    """modsearch 总开关（设置 → 联网搜索）；关闭后引擎链与云端抓页都不再调用。"""
    cfg = cfg or load_config()
    return bool(cfg.get("search_modsearch_enabled", True))


# ---------------------------------------------------------------- 冷却
def _state():
    data = read_json(STATE_FILE, default=None)
    return data if isinstance(data, dict) else {}


def cooldown_left(name):
    """这个引擎还有多少秒处于冷却；0 = 可用。"""
    cooling = _state().get("cooling")
    until = float((cooling or {}).get(name) or 0)
    return max(0.0, until - time.time())


def _set_cooldown(name, ttl=QUOTA_COOLDOWN):
    state = _state()
    cooling = state.get("cooling") if isinstance(state.get("cooling"), dict) else {}
    cooling[name] = time.time() + max(60, int(ttl or QUOTA_COOLDOWN))
    state["cooling"] = cooling
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        write_json(STATE_FILE, state)
    except Exception:                        # noqa: BLE001  冷却只是优化，写失败不影响搜索
        pass


def clear_cooldown():
    try:
        write_json(STATE_FILE, {})
    except Exception:                        # noqa: BLE001
        pass


# ---------------------------------------------------------------- 请求
def _call(method, url, payload=None, headers=None, timeout=DEFAULT_TIMEOUT, params=None):
    try:
        resp = requests.request(method, url, json=payload, headers=headers,
                                params=params, timeout=timeout)
    except requests.RequestException as e:
        raise EngineError("网络错误：%s" % e, kind="network")
    if resp.status_code >= 500:
        raise EngineError("服务端 %d" % resp.status_code, kind="network")
    if resp.status_code in (401, 403):
        raise EngineError("鉴权失败（HTTP %d）" % resp.status_code, kind="auth")
    if resp.status_code in (402, 429):
        raise EngineError("额度用尽（HTTP %d）" % resp.status_code, kind="quota")
    if resp.status_code >= 400:
        raise EngineError("请求失败（HTTP %d）：%s"
                          % (resp.status_code, (resp.text or "")[:200]), kind="network")
    try:
        return resp.json()
    except ValueError:
        raise EngineError("返回内容不是 JSON", kind="network")


def _item(title, url, snippet):
    host = ""
    try:
        host = urlparse(str(url or "")).hostname or ""
    except Exception:                        # noqa: BLE001
        pass
    text = str(snippet or "").strip()
    if len(text) > SNIPPET_MAX:
        text = text[:SNIPPET_MAX] + "…"
    return {"title": str(title or "").strip(), "url": str(url or "").strip(),
            "site": host, "snippet": text}


# ---------------------------------------------------------------- 引擎
def firecrawl_search(query, count=8, key=""):
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = "Bearer " + key
    try:
        limit = max(1, min(int(count or 8), 20))
    except (TypeError, ValueError):
        limit = 8
    data = _call("POST", FIRECRAWL_BASE + "/v2/search", {
        "query": query, "limit": limit, "sources": ["web"],
        "timeout": DEFAULT_TIMEOUT * 1000,
    }, headers)
    rows = ((data.get("data") or {}).get("web") or []) if isinstance(data, dict) else []
    return [_item(r.get("title"), r.get("url"), r.get("description"))
            for r in rows if isinstance(r, dict) and r.get("url")]


def keenable_search(query, count=8, key=""):
    headers = {"content-type": "application/json"}
    if key:
        headers["x-api-key"] = key
        url = KEENABLE_BASE + "/v1/search"
    else:
        # keyless 端点要求用这个 header 标明调用方
        headers["x-keenable-title"] = "galgame-monster"
        url = KEENABLE_BASE + "/v1/search/public"
    try:
        limit = max(1, min(int(count or 8), 50))
    except (TypeError, ValueError):
        limit = 8
    data = _call("POST", url, {"query": query, "max_results": limit}, headers)
    items = []
    for r in (data.get("results") or []) if isinstance(data, dict) else []:
        if not isinstance(r, dict) or not r.get("url"):
            continue
        it = _item(r.get("title"), r.get("url"), r.get("snippet") or r.get("description"))
        if r.get("published_at"):
            it["published_at"] = str(r["published_at"])
        items.append(it)
    return items


def youcom_search(query, count=8, key=""):
    """You.com 目前只有 keyless 端点；key 参数留空即可（保留签名便于以后接 keyed）。"""
    try:
        limit = max(1, min(int(count or 8), 50))
    except (TypeError, ValueError):
        limit = 8
    data = _call("GET", YOUCOM_URL, None, {"accept": "application/json"},
                 params={"query": query, "count": limit})
    items = []
    for r in (((data.get("results") or {}).get("web")) or []) if isinstance(data, dict) else []:
        if not isinstance(r, dict) or not r.get("url"):
            continue
        snippet = r.get("snippets")
        snippet = snippet[0] if isinstance(snippet, list) and snippet else r.get("description")
        it = _item(r.get("title"), r.get("url"), snippet)
        if r.get("page_age"):
            it["published_at"] = str(r["page_age"])
        items.append(it)
    return items


_ENGINES = {"firecrawl": firecrawl_search,
            "keenable": keenable_search,
            "youcom": youcom_search}


# ---------------------------------------------------------------- 自动链
def auto_search(query, count=8, cfg=None):
    """按 CHAIN 顺序检索；失败或结果明显跑题时换下一个。

    全失败时抛 EngineError（带每个引擎的原因）；如果所有引擎都只给了低质量结果，
    返回其中质量最高的一组 —— 有结果总比空手好。
    """
    cfg = cfg or load_config()
    if not modsearch_enabled(cfg):
        raise EngineError("modsearch 已关闭（设置 → 联网与数据源 → 联网搜索）", kind="disabled")
    keys = engine_keys(cfg)
    enabled = engine_enabled(cfg)
    errors = []
    best, best_q, best_name = None, -1.0, ""
    for name in CHAIN:
        if not enabled.get(name, True):
            continue
        wait = cooldown_left(name)
        if wait > 0:
            errors.append("%s 冷却中（还剩 %d 分钟）" % (name, int(wait // 60) + 1))
            continue
        try:
            items = _ENGINES[name](query, count, keys.get(name) or "")
        except EngineError as e:
            errors.append("%s: %s" % (name, e))
            if e.kind in ("quota", "auth"):
                _set_cooldown(name, e.ttl)
            continue
        except Exception as e:               # noqa: BLE001  单个引擎异常不能拖垮整条链
            errors.append("%s: %s" % (name, e))
            continue
        if not items:
            errors.append("%s: 没有结果" % name)
            continue
        try:
            from app.search_rank import provider_quality
            quality = provider_quality(items, query)
        except Exception:                    # noqa: BLE001  排序层异常不影响搜索本身
            quality = 1.0
        if quality >= QUALITY_MIN:
            for it in items:
                it["engine"] = name
            if errors:
                print("[搜索] 已降级到 %s（%s）" % (name, "；".join(errors)))
            return items
        # 明显跑题：先记下最优的一组，继续试后面的引擎
        errors.append("%s: 结果与查询相关度低（%.2f）" % (name, quality))
        if quality > best_q:
            best, best_q, best_name = items, quality, name
    if best:
        for it in best:
            it["engine"] = best_name
        print("[搜索] 引擎结果相关度都偏低，返回最优的一组（%s）" % "；".join(errors))
        return best
    raise EngineError("免注册引擎都没能返回结果 → " + "；".join(errors), kind="all")


def cloud_fetch(url, timeout=SCRAPE_TIMEOUT, key="", cfg=None):
    """Firecrawl 云端抓页，返回 markdown；不可用 / 失败返回空串（调用方回落本地）。

    私网判断由调用方（app/search.py）负责：这里只保证不把 Key 写进日志。
    """
    url = str(url or "").strip()
    if not url.lower().startswith("http"):
        return ""
    cfg = cfg or load_config()
    if not modsearch_enabled(cfg) or cooldown_left("firecrawl") > 0:
        return ""
    key = key or engine_keys(cfg).get("firecrawl", "")
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = "Bearer " + key
    try:
        data = _call("POST", FIRECRAWL_BASE + "/v2/scrape", {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "maxAge": 0,                 # 不读多天缓存：查资料不能拿到旧页面
            "storeInCache": False,       # 也不写进对方缓存 / 索引
            "skipTlsVerification": False,
            "timeout": max(1000, int(timeout * 1000)),
        }, headers, timeout=timeout)
    except EngineError as e:
        if e.kind in ("quota", "auth"):
            _set_cooldown("firecrawl", e.ttl)
        return ""
    d = data.get("data") if isinstance(data, dict) else None
    return str((d or {}).get("markdown") or "").strip()
