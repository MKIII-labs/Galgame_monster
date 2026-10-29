# -*- coding: utf-8 -*-
"""联网搜索 / 识图通道选择：默认用聊天模型，无聊天密钥或用户勾选时回落独立 API。"""
from app import config, search


def _cfg(**kw):
    base = {"chat_api_key": "k",
            "chat_url": "https://api.deepseek.com/v1/chat/completions",
            "chat_model": "deepseek-flash",
            "search_provider": "volc", "search_api_key": "sk"}
    base.update(kw)
    return base


def test_chat_search_available_for_deepseek(monkeypatch):
    monkeypatch.setattr(search, "load_config", lambda: _cfg())
    assert search.chat_search_available() is True
    assert search.effective_search_provider() == "deepseek"
    provider, url, key, _count = search.get_search_conf()
    assert provider == "deepseek"
    assert url == "https://api.deepseek.com/v1/tools/search"
    assert key == "k"                      # 复用聊天密钥，不用 search_api_key


def test_search_prefer_api_uses_own_provider(monkeypatch):
    monkeypatch.setattr(search, "load_config",
                        lambda: _cfg(search_prefer_api=True))
    assert search.effective_search_provider() == "volc"
    provider, url, key, _count = search.get_search_conf()
    assert provider == "volc"
    assert key == "sk"
    assert "feedcoop" in url


def test_chat_search_requires_deepseek_host(monkeypatch):
    monkeypatch.setattr(search, "load_config", lambda: _cfg(
        chat_url="https://api.openai.com/v1/chat/completions",
        chat_model="gpt-4o"))
    assert search.chat_search_available() is False
    assert search.effective_search_provider() == "volc"


def test_search_web_deepseek_prefers_summary(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"title": "T", "url": "U", "site_name": "S",
                                 "summary": "SUM", "snippet": "SNIP"}]}

    monkeypatch.setattr(search.requests, "post", lambda *a, **k: _Resp())
    items = search.search_web("q", "deepseek", "https://api.deepseek.com/v1/tools/search",
                              "k", 3)
    assert items == [{"title": "T", "url": "U", "site": "S", "snippet": "SUM"}]


def test_vision_uses_chat_model_by_default(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {
        "chat_api_key": "k", "chat_model": "deepseek-flash",
        "chat_url": "https://api.deepseek.com/v1/chat/completions"})
    assert config.use_chat_for_vision() is True


def test_vision_falls_back_without_chat_key(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {
        "chat_api_key": "", "vision_api_key": "v"})
    assert config.use_chat_for_vision() is False


def test_vision_prefer_api_wins(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {
        "chat_api_key": "k", "chat_model": "gpt-4o",
        "chat_url": "https://api.openai.com/v1/chat/completions",
        "vision_prefer_api": True})
    assert config.use_chat_for_vision() is False
