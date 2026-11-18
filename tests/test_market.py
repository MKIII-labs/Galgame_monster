# -*- coding: utf-8 -*-
"""插件市场：DSH 索引的搜索 / 分页 / 缓存与离线回退；官方市场索引的本地 / 远端读取。"""
import json
import os

import pytest

from app import market

DSH_RAW = {
    "updated": "2026-10-25",
    "count": 2,
    "categories": {"tools": {"en": "Tools", "zh": "工具"}},
    "plugins": [
        {"name": "dsh-alpha", "owner": "a", "url": "https://x/1", "page": "https://x/p/1",
         "category": "tools", "description": {"en": "alpha tool", "zh": "阿尔法工具"},
         "capabilities": ["shell"], "install": "dsh plugin add dsh-alpha", "stars": 3},
        {"name": "dsh-beta", "owner": "b", "url": "https://x/2", "page": "https://x/p/2",
         "category": "ui", "description": {"en": "beta", "zh": "贝塔"},
         "capabilities": [], "install": "dsh plugin add dsh-beta"},
    ],
}


class _Resp:
    def __init__(self, data):
        self.content = json.dumps(data).encode("utf-8")
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _Handler:
    """最小 API handler 桩：只需要 _body / _json。"""

    def __init__(self, body):
        self.body = body
        self.payload = None
        self.code = 200

    def _body(self):
        return self.body

    def _json(self, obj, code=200):
        self.payload = obj
        self.code = code
        return obj


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = str(tmp_path / "cache" / "dsh.json")
    monkeypatch.setattr(market, "CACHE_FILE", path)
    return path


def test_dsh_page_and_cache(cache, monkeypatch):
    monkeypatch.setattr(market.requests, "get", lambda *a, **k: _Resp(DSH_RAW))
    out = market.dsh_page(size=12)
    assert out["total"] == 2
    assert out["count"] == 2
    assert out["items"][0]["name"] == "dsh-alpha"
    assert out["items"][0]["description"]["zh"] == "阿尔法工具"
    assert out["categories"] == {"tools": "工具"}      # {en, zh} 要压成一行文字
    assert out["stale"] is False
    assert os.path.isfile(cache)                        # 已写缓存

    # 分类 / 关键词
    assert market.dsh_page(category="tools")["total"] == 1
    assert market.dsh_page(q="贝塔")["total"] == 1

    # 断网：用缓存，标记 stale
    def boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(market.requests, "get", boom)
    out2 = market.dsh_page(force=True)
    assert out2["stale"] is True
    assert out2["total"] == 2


def test_dsh_without_cache_raises(cache, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(market.requests, "get", boom)
    with pytest.raises(RuntimeError):
        market.dsh_page()


def test_market_api_rejects_local_ops(cache):
    from app.api import market as api_market

    h = _Handler({"op": "list"})
    api_market._api_market(h)
    assert h.code == 400
    assert h.payload["ok"] is False


def test_route_registered():
    from app.api import market as api_market          # noqa: F401  触发自注册
    from app.registry import get
    assert get("POST", "/api/market") is not None


# ------------------------------------------------------------
# 官方插件市场索引
# ------------------------------------------------------------
INDEX_RAW = {
    "schema": 1,
    "updated": "2026-11-01",
    "items": [
        {"id": "alpha", "name": "阿尔法", "version": "1.0.0", "author": "me",
         "description": "示例", "permissions": ["read"],
         "url": "https://x/alpha.zip", "sha256": "A" * 64, "size": "1024"},
        {"id": "beta", "name": "贝塔", "url": "http://x/beta.zip", "sha256": "bad"},
        {"id": "bad id", "name": "坏"},
    ],
}


@pytest.fixture
def index_env(tmp_path, monkeypatch):
    monkeypatch.setattr(market, "PLUGIN_INDEX_FILE", str(tmp_path / "market.json"))
    monkeypatch.setattr(market, "PLUGIN_INDEX_CACHE",
                        str(tmp_path / "cache" / "plugin_index.json"))
    monkeypatch.setattr(market, "load_config",
                        lambda: {"plugin_index_url": "", "plugin_index_ttl": 3600})
    return tmp_path


def test_plugin_index_local(index_env):
    (index_env / "market.json").write_text(json.dumps(INDEX_RAW), encoding="utf-8")
    out = market.plugin_index()
    assert out["source"] == "local"
    assert out["error"] == ""
    assert out["updated"] == "2026-11-01"
    # 非法 id 被丢弃；http 下载地址与非法 sha256 被清空
    assert [i["id"] for i in out["items"]] == ["alpha", "beta"]
    assert out["items"][0]["sha256"] == "a" * 64
    assert out["items"][0]["size"] == 1024
    assert out["items"][1]["url"] == ""
    assert out["items"][1]["sha256"] == ""


def test_plugin_index_local_broken(index_env):
    (index_env / "market.json").write_text("{bad", encoding="utf-8")
    out = market.plugin_index()
    assert out["items"] == []
    assert out["error"]
    (index_env / "market.json").write_text('{"items": {}}', encoding="utf-8")
    assert market.plugin_index()["error"]


def test_plugin_index_remote_cache_and_fallback(index_env, monkeypatch):
    monkeypatch.setattr(market, "load_config",
                        lambda: {"plugin_index_url": "https://x/index.json",
                                 "plugin_index_ttl": 3600})
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(INDEX_RAW)

    monkeypatch.setattr(market.requests, "get", fake_get)
    out = market.plugin_index()
    assert out["source"] == "remote"
    assert out["stale"] is False
    assert [i["id"] for i in out["items"]] == ["alpha", "beta"]
    assert os.path.isfile(market.PLUGIN_INDEX_CACHE)

    market.plugin_index()                       # TTL 内命中缓存，不再请求
    assert len(calls) == 1

    def boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(market.requests, "get", boom)
    out2 = market.plugin_index(force=True)      # 断网：回退缓存
    assert out2["stale"] is True
    assert out2["error"]
    assert len(out2["items"]) == 2


def test_plugin_index_rejects_http(index_env, monkeypatch):
    monkeypatch.setattr(market, "load_config",
                        lambda: {"plugin_index_url": "http://x/index.json",
                                 "plugin_index_ttl": 3600})
    with pytest.raises(RuntimeError):
        market.plugin_index()


def test_market_api_official(index_env):
    from app.api import market as api_market

    (index_env / "market.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    h = _Handler({"op": "official"})
    api_market._api_market(h)
    assert h.payload["ok"] is True
    assert h.payload["items"] == []
    assert h.payload["source"] == "local"
