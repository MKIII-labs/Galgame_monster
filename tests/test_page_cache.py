# -*- coding: utf-8 -*-
"""网页正文缓存：同 URL + 检索式命中，不同检索式不串。"""
import pytest

from app import page_cache


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(page_cache, "CACHE_FILE", str(tmp_path / "page_cache.json"))
    page_cache.reset()
    yield
    page_cache.reset()


def test_put_get_and_key_scope(env):
    k = page_cache.key("https://a.example/x", "jina", "检索 式", 6000)
    page_cache.put(k, "正文 A", [{"quote": "片段"}])
    got = page_cache.get(k)
    assert got is not None
    assert got[0] == "正文 A"
    assert got[1][0]["quote"] == "片段"
    # 检索式 / 抓取档位不同不能命中同一条
    assert page_cache.get(page_cache.key("https://a.example/x", "jina", "另一个", 6000)) is None
    assert page_cache.get(page_cache.key("https://a.example/x", "direct", "检索 式", 6000)) is None


def test_expired_entry_is_dropped(env):
    k = page_cache.key("https://b.example", "jina", "q", 100)
    page_cache.put(k, "正文 B", [], ttl=60)
    page_cache._MEM[k]["ts"] -= 120          # 模拟过期
    assert page_cache.get(k) is None
    assert k not in page_cache._MEM


def test_fetch_doc_reuses_cached_page(env, monkeypatch):
    from app import search
    calls = []

    def fake(url, **kw):
        calls.append(url)
        return "正文 C", [{"quote": "片段 C"}]

    monkeypatch.setattr(search, "_fetch_doc_uncached", fake)
    a = search._fetch_doc("https://c.example/x", "jina", query="q", chars=100)
    b = search._fetch_doc("https://c.example/x", "jina", query="q", chars=100)
    assert a == b
    assert len(calls) == 1
    # 换检索式重新抓
    search._fetch_doc("https://c.example/x", "jina", query="q2", chars=100)
    assert len(calls) == 2
