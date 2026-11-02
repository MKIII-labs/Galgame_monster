# -*- coding: utf-8 -*-
"""插件市场：DSH 索引的搜索 / 分页 / 缓存与离线回退（本地 Skill 模块已移除）。"""
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

    class _H:
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

    h = _H({"op": "list"})
    api_market._api_market(h)
    assert h.code == 400
    assert h.payload["ok"] is False


def test_route_registered():
    from app.api import market as api_market          # noqa: F401  触发自注册
    from app.registry import get
    assert get("POST", "/api/market") is not None
