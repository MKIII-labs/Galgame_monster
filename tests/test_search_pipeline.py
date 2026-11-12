# -*- coding: utf-8 -*-
"""P0 检索策略：Query Planner / canonical 去重 / 排序 / passage 证据 / 引用审计。"""
from datetime import datetime

import pytest

from app import (research, search, search_cache, search_engines, search_plan,
                 search_rank, search_stats)
from app.search_evidence import extract_evidence


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch):
    """单测固定走纯词法排序：本地向量模型在机器上存在时会让排序结果不稳定。"""
    monkeypatch.setattr(search_rank, "load_config",
                        lambda: {"search_semantic_rank": False})


# ---------------------------------------------------------------- Query Planner

def test_plan_freshness_adds_year_and_caps_queries():
    p = search_plan.plan("帮我查一下最新价格", ["比特币 最新价格"],
                         now=datetime(2026, 1, 1))
    assert p["intent"] == "fresh"
    assert p["freshness"]
    assert any("2026" in q for q in p["queries"])
    assert 1 <= len(p["queries"]) <= search_plan.MAX_QUERIES


def test_plan_compare_expands_entities():
    p = search_plan.plan("", ["React vs Vue 性能"], now=datetime(2026, 1, 1))
    assert p["intent"] == "compare"
    assert any(q.strip() == "React" for q in p["queries"])
    assert any("Vue" in q for q in p["queries"])


def test_plan_dedupes_identical_queries():
    p = search_plan.plan("", ["Python PDF", "python  pdf"], now=datetime(2026, 1, 1))
    assert len(p["queries"]) == 1


def test_plan_keeps_model_query_when_single():
    p = search_plan.plan("", ["python pdf 官方文档"], now=datetime(2026, 1, 1))
    assert p["queries"][0] == "python pdf 官方文档"


def test_fallback_query_for_docs():
    plan = {"intent": "docs", "queries": ["requests timeout"]}
    q = search_plan.fallback_query(plan, "")
    assert q and "官方文档" in q


def test_fallback_query_returns_empty_when_no_change():
    plan = {"intent": "general", "queries": ["python"]}
    assert search_plan.fallback_query(plan, "") == ""


# ---------------------------------------------------------------- 缓存

def test_search_cache_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(search_cache, "CACHE_FILE", str(tmp_path / "cache.json"))
    search_cache.put("p", "u", "q", 3, [{"title": "T", "url": "https://e.com"}], ttl=60)
    got = search_cache.get("p", "u", "q", 3)
    assert got and got[0]["title"] == "T"
    assert search_cache.get("p", "u", "q", 3, enabled=False) is None


def test_search_multi_reuses_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(search_cache, "CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(search, "load_config",
                        lambda: {"search_cache_enabled": True, "search_cache_ttl": 3600})
    calls = []

    def fake(q, provider, url, key, count):
        calls.append(q)
        return [{"title": "T", "url": "https://e.com/x", "snippet": "s"}]

    monkeypatch.setattr(search, "search_web", fake)
    search.search_multi(["cachetest"], "auto", "", "", 3)
    search.search_multi(["cachetest"], "auto", "", "", 3)
    assert calls == ["cachetest"]


# ---------------------------------------------------------------- 抓取分发

def test_fetch_doc_dispatches_pdf(monkeypatch):
    monkeypatch.setattr(search, "_fetch_pdf",
                        lambda *a, **k: ("PDF正文", [{"quote": "PDF正文"}]))
    content, ev = search._fetch_doc("https://e.com/a.pdf")
    assert content == "PDF正文" and ev


def test_fetch_doc_dispatches_github(monkeypatch):
    monkeypatch.setattr(search, "_fetch_github",
                        lambda *a, **k: ("README 正文", [{"quote": "README 正文"}]))
    content, ev = search._fetch_doc("https://github.com/o/r")
    assert content == "README 正文" and ev


# ---------------------------------------------------------------- canonical / 排序

def test_canonical_dedupe():
    items = [
        {"title": "A", "url": "https://www.example.com/path/?utm_source=x&a=1#frag"},
        {"title": "A2", "url": "http://example.com/path?a=1"},
    ]
    out = search_rank.dedupe(items)
    assert len(out) == 1
    assert out[0]["canonical_url"] == "https://example.com/path?a=1"


def test_rank_prefers_authority_and_limits_domain():
    items = [
        {"title": "Node.js 官方文档", "url": "https://nodejs.org/en/docs",
         "snippet": "Node.js 官方文档 API"},
        {"title": "某博客", "url": "https://random-blog.com/node", "snippet": "Node.js 随便写"},
        {"title": "另一个博客", "url": "https://random-blog.com/node2", "snippet": "Node.js 教程"},
        {"title": "内容农场", "url": "https://baijiahao.baidu.com/s?id=1", "snippet": "Node.js"},
    ]
    out = search_rank.rank_hits(items, ["Node.js 文档"], intent="docs")
    assert out[0]["url"].startswith("https://nodejs.org")
    assert len(out) == 4                       # 不丢结果
    assert sum(1 for it in out[:3] if "random-blog.com" in it["url"]) <= 2


def test_rank_prefers_fresh_for_news_intent():
    items = [
        {"title": "价格 100", "url": "https://a.example/old", "snippet": "价格",
         "published_at": "2024-01-01T00:00:00Z"},
        {"title": "价格 100", "url": "https://b.example/new", "snippet": "价格",
         "published_at": "2026-11-10T00:00:00Z"},
    ]
    out = search_rank.rank_hits(items, ["价格"], intent="fresh", freshness="day")
    assert out[0]["url"].endswith("/new")


def test_rank_uses_semantic_scores(monkeypatch):
    monkeypatch.setattr(search_rank, "_semantic_scores",
                        lambda queries, rows: [0.95 if "语义" in it["title"] else 0.0
                                               for it in rows])
    items = [
        {"title": "普通结果", "url": "https://a.example/x", "snippet": "query words"},
        {"title": "语义结果", "url": "https://b.example/y", "snippet": "query words"},
    ]
    out = search_rank.rank_hits(items, ["query words"])
    assert out[0]["title"] == "语义结果"
    assert out[0]["rank_semantic"] == 0.95


def test_stats_summary_percentiles():
    rows = [
        {"ms": 100, "hits": 5, "fetched": 3, "research": True},
        {"ms": 300, "hits": 0, "fetched": 0, "research": False},
    ]
    s = search_stats.summary(rows)
    assert s["count"] == 2
    assert s["p95_ms"] == 300
    assert s["empty_rate"] == 0.5


# ---------------------------------------------------------------- 质量感知 fallback

def test_auto_search_skips_off_topic_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(search_engines, "STATE_FILE", str(tmp_path / "engines.json"))
    monkeypatch.setattr(search_engines, "load_config", lambda: {})
    calls = []

    def firecrawl(q, n, k):
        calls.append("firecrawl")
        return [{"title": "完全无关", "url": "https://spam.example/a", "snippet": "无关"}]

    def keenable(q, n, k):
        calls.append("keenable")
        return [{"title": "Python 官方文档", "url": "https://docs.python.org/3/",
                 "snippet": "Python documentation"}]

    monkeypatch.setattr(search_engines, "_ENGINES", {
        "firecrawl": firecrawl,
        "keenable": keenable,
        "youcom": lambda q, n, k: [],
    })
    items = search_engines.auto_search("Python documentation", 5)
    assert calls[:2] == ["firecrawl", "keenable"]
    assert "docs.python.org" in items[0]["url"]
    assert items[0]["engine"] == "keenable"


def test_auto_search_returns_best_low_quality(monkeypatch, tmp_path):
    monkeypatch.setattr(search_engines, "STATE_FILE", str(tmp_path / "engines.json"))
    monkeypatch.setattr(search_engines, "load_config", lambda: {})
    monkeypatch.setattr(search_engines, "_ENGINES", {
        "firecrawl": lambda q, n, k: [{"title": "无关", "url": "https://spam.example/a",
                                       "snippet": "完全无关"}],
        "keenable": lambda q, n, k: [],
        "youcom": lambda q, n, k: [],
    })
    items = search_engines.auto_search("Python documentation", 3)
    assert items and items[0]["engine"] == "firecrawl"


# ---------------------------------------------------------------- passage 证据

def test_extract_evidence_keeps_quote_and_lines():
    text = "\n".join([
        "导航",
        "",
        "第一段无关内容",
        "",
        "## 价格",
        "比特币 2026 年价格是 100000 美元。",
        "",
        "第二段无关内容",
    ])
    content, ev = extract_evidence(text, "比特币 价格", max_chars=300, max_passages=2)
    assert "100000" in content
    assert any("100000" in e["quote"] for e in ev)
    hit = [e for e in ev if "100000" in e["quote"]][0]
    assert hit["line_start"] == 5
    assert hit["line_end"] == 6


# ---------------------------------------------------------------- 引用审计

def test_audit_citations_number_and_existence(monkeypatch, tmp_path):
    monkeypatch.setattr(research, "DATA_DIR", str(tmp_path))
    led = research.Ledger("audit")
    led.add_sources([{
        "title": "T", "url": "https://e.com/1", "snippet": "",
        "evidence": [{"quote": "比特币价格是 100000 美元，涨幅 3.5%",
                      "line_start": 10, "line_end": 10}],
    }])
    ok = led.audit_citations("据 [1]，比特币价格是 100000 美元。")
    assert ok["bad"] == [] and not ok["unsupported"]

    bad_num = led.audit_citations("据 [1]，比特币价格是 120000 美元。")
    assert bad_num["unsupported"]
    assert "120000" in bad_num["unsupported"][0]["missing"]

    bad_ref = led.audit_citations("据 [2]，价格是 100000 美元。")
    assert bad_ref["bad"] == [2]

    bad_ident = led.audit_citations("据 [1]，运行 `pip install foo` 即可。")
    assert bad_ident["unsupported"]
    assert any("pip install foo" in x for x in bad_ident["unsupported"][0]["missing"])


def test_build_search_context_uses_evidence_and_date():
    ctx = search.build_search_context("q", [{
        "title": "T", "url": "https://e.com", "n": 1,
        "published_at": "2026-11-01",
        "evidence": [{"quote": "原文引句", "line_start": 3, "line_end": 4}],
    }])
    assert "原文引句" in ctx
    assert "L3–L4" in ctx
    assert "2026-11-01" in ctx
