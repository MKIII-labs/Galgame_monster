# -*- coding: utf-8 -*-
"""免注册引擎链：字段收敛、失败转移、冷却、云端抓页与错误上抛。"""
import pytest

from app import search, search_cache, search_engines, search_rank


class _Resp:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = text

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """冷却文件写到临时目录，并清掉可能存在的真实环境变量 Key。"""
    monkeypatch.setattr(search_engines, "STATE_FILE", str(tmp_path / "engines.json"))
    monkeypatch.setattr(search_engines, "load_config", lambda: {})
    monkeypatch.setattr(search_cache, "CACHE_FILE", str(tmp_path / "search_cache.json"))
    monkeypatch.setattr(search_rank, "load_config",
                        lambda: {"search_semantic_rank": False})
    for env in search_engines.ENV_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    return tmp_path


def test_firecrawl_search_maps_and_truncates(monkeypatch):
    long_desc = "x" * 2000
    seen = {}

    def fake(method, url, json=None, headers=None, params=None, timeout=20):
        seen["url"] = url
        seen["headers"] = headers
        return _Resp(200, {"success": True, "data": {"web": [
            {"title": "Node", "url": "https://nodejs.org/en", "description": long_desc},
            {"title": "no url"},                       # 没有 url 的条目应被丢弃
        ]}})

    monkeypatch.setattr(search_engines.requests, "request", fake)
    items = search_engines.firecrawl_search("node lts", 5)
    assert seen["url"].endswith("/v2/search")
    assert "authorization" not in seen["headers"]      # keyless：不带鉴权头
    assert len(items) == 1
    assert items[0]["site"] == "nodejs.org"
    assert len(items[0]["snippet"]) <= search_engines.SNIPPET_MAX + 1


def test_keenable_keyless_endpoint_and_published(monkeypatch):
    seen = {}

    def fake(method, url, json=None, headers=None, params=None, timeout=20):
        seen["url"], seen["headers"], seen["json"] = url, headers, json
        return _Resp(200, {"results": [
            {"title": "A", "url": "https://a.com/p", "snippet": "sa",
             "published_at": "2026-10-01T00:00:00Z"},
            {"title": "B", "url": "https://b.com", "description": "db"},
        ]})

    monkeypatch.setattr(search_engines.requests, "request", fake)
    items = search_engines.keenable_search("q", 6)
    assert seen["url"].endswith("/v1/search/public")
    assert seen["headers"]["x-keenable-title"] == "galgame-monster"
    assert seen["json"]["max_results"] == 6
    assert items[0]["published_at"].startswith("2026-10-01")
    assert items[1]["snippet"] == "db"                  # 没 snippet 时回落 description


def test_youcom_mapping(monkeypatch):
    def fake(method, url, json=None, headers=None, params=None, timeout=20):
        assert method == "GET" and params["count"] == 3
        return _Resp(200, {"results": {"web": [
            {"title": "Y", "url": "https://y.com", "snippets": ["first", "second"],
             "page_age": "2 days"},
        ]}})

    monkeypatch.setattr(search_engines.requests, "request", fake)
    items = search_engines.youcom_search("q", 3)
    assert items[0]["snippet"] == "first"
    assert items[0]["published_at"] == "2 days"


def test_auto_search_falls_over_and_cools_quota_engine(monkeypatch):
    calls = []

    def fake(method, url, json=None, headers=None, params=None, timeout=20):
        calls.append(url)
        if "firecrawl" in url:
            return _Resp(429, {"error": "quota"})
        return _Resp(200, {"results": [
            {"title": "K", "url": "https://k.com", "snippet": "kk"}]})

    monkeypatch.setattr(search_engines.requests, "request", fake)
    items = search_engines.auto_search("q", 4)
    assert items[0]["engine"] == "keenable"
    assert search_engines.cooldown_left("firecrawl") > 0

    calls.clear()
    items = search_engines.auto_search("q2", 4)         # 冷却期内不再打 firecrawl
    assert all("firecrawl" not in u for u in calls)
    assert items[0]["engine"] == "keenable"


def test_auto_search_respects_disabled_engine(monkeypatch):
    monkeypatch.setattr(search_engines, "engine_enabled",
                        lambda cfg=None: {"firecrawl": False, "keenable": True, "youcom": False})
    seen = []

    def fake(method, url, json=None, headers=None, params=None, timeout=20):
        seen.append(url)
        return _Resp(200, {"results": [{"title": "K", "url": "https://k.com"}]})

    monkeypatch.setattr(search_engines.requests, "request", fake)
    search_engines.auto_search("q", 2)
    assert seen and all("keenable" in u for u in seen)


def test_auto_search_raises_with_every_reason(monkeypatch):
    def fake(method, url, json=None, headers=None, params=None, timeout=20):
        return _Resp(500, {}, text="boom")

    monkeypatch.setattr(search_engines.requests, "request", fake)
    with pytest.raises(search_engines.EngineError) as ei:
        search_engines.auto_search("q", 2)
    msg = str(ei.value)
    for name in ("firecrawl", "keenable", "youcom"):
        assert name in msg


def test_master_switch_off_blocks_chain_and_cloud(monkeypatch):
    monkeypatch.setattr(search_engines, "load_config",
                        lambda: {"search_modsearch_enabled": False})

    def fake(method, url, json=None, headers=None, params=None, timeout=20):
        raise AssertionError("关闭后不应该发出任何请求")

    monkeypatch.setattr(search_engines.requests, "request", fake)
    with pytest.raises(search_engines.EngineError) as ei:
        search_engines.auto_search("q", 2)
    assert "已关闭" in str(ei.value)
    assert search_engines.cloud_fetch("https://e.com") == ""


def test_master_switch_defaults_on(monkeypatch):
    monkeypatch.setattr(search_engines, "load_config", lambda: {})
    assert search_engines.modsearch_enabled() is True


def test_cloud_fetch_returns_markdown(monkeypatch):
    def fake(method, url, json=None, headers=None, params=None, timeout=20):
        assert url.endswith("/v2/scrape")
        assert json["storeInCache"] is False and json["maxAge"] == 0
        return _Resp(200, {"success": True, "data": {"markdown": "# 标题\n正文"}})

    monkeypatch.setattr(search_engines.requests, "request", fake)
    assert search_engines.cloud_fetch("https://e.com") == "# 标题\n正文"


def test_fetch_one_skips_cloud_for_private_target(monkeypatch):
    called = []
    monkeypatch.setattr(search_engines, "cloud_fetch",
                        lambda *a, **k: called.append(a) or "云端正文")
    monkeypatch.setattr(search, "public_url_error", lambda url: "拒绝私网")
    monkeypatch.setattr(search, "_public_get", lambda *a, **k: None)
    assert search._fetch_one("http://127.0.0.1/x", reader="jina") == ""
    assert not called


def test_fetch_one_prefers_cloud(monkeypatch):
    monkeypatch.setattr(search_engines, "cloud_fetch", lambda *a, **k: "云端正文")
    monkeypatch.setattr(search, "public_url_error", lambda url: "")
    monkeypatch.setattr(search, "_public_get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该回落")))
    assert "云端正文" in search._fetch_one("https://e.com", reader="jina")


def test_search_web_auto_delegates(monkeypatch):
    monkeypatch.setattr(search_engines, "auto_search",
                        lambda q, n: [{"title": "T", "url": "https://t", "site": "t",
                                       "snippet": "s", "engine": "firecrawl"}])
    items = search.search_web("q", "auto", "", "", 5)
    assert items[0]["engine"] == "firecrawl"


def test_search_multi_raises_when_all_queries_fail(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("firecrawl: 额度用尽")

    monkeypatch.setattr(search, "search_web", boom)
    with pytest.raises(RuntimeError) as ei:
        search.search_multi(["a", "b"], "auto", "", "", 3)
    assert "额度用尽" in str(ei.value)


def test_search_multi_keeps_partial_results(monkeypatch):
    def flaky(q, provider, url, key, count):
        if q == "bad":
            raise RuntimeError("boom")
        return [{"title": q, "url": "https://" + q, "site": q, "snippet": "s"}]

    monkeypatch.setattr(search, "search_web", flaky)
    out = search.search_multi(["good", "bad"], "auto", "", "", 3)
    assert [it["url"] for it in out] == ["https://good"]
