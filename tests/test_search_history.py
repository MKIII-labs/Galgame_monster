# -*- coding: utf-8 -*-
"""搜索来源落库：刷新后重建搜索 trace 需要 hits + 抓取过的页面。"""
from app.api.chat import TurnResult, _build_new_assistant_message, _merge_continuation


def test_build_message_persists_search_pages():
    tr = TurnResult(content="ok", sources=[
        {"title": "A", "url": "https://a.example", "site": "a", "snippet": "s",
         "content": "body"},
        {"title": "B", "url": "https://b.example", "site": "b", "snippet": "s"},
    ])
    msg = _build_new_assistant_message({}, tr)
    assert len(msg["sources"]) == 2
    assert all("content" not in s for s in msg["sources"])   # 正文不入库
    assert msg["search"]["hits"] == 2
    assert [p["url"] for p in msg["search"]["pages"]] == ["https://a.example"]


def test_merge_continuation_adds_search_pages():
    last = {"role": "assistant", "content": "x",
            "sources": [{"title": "A", "url": "https://a.example"}]}
    tr = TurnResult(content="y", sources=[
        {"title": "B", "url": "https://b.example", "snippet": "s", "content": "body"},
    ])
    _merge_continuation(last, tr)
    assert [s["url"] for s in last["sources"]] == [
        "https://a.example", "https://b.example"]
    assert [p["url"] for p in last["search"]["pages"]] == ["https://b.example"]


def test_build_message_dedupes_sources_and_keeps_n():
    tr = TurnResult(content="ok", sources=[
        {"title": "A", "url": "https://a.example", "n": 3, "snippet": "s"},
        {"title": "A again", "url": "https://a.example", "n": 3, "snippet": "s"},
        {"title": "B", "url": "https://b.example", "n": 4, "snippet": "s"},
    ])
    msg = _build_new_assistant_message({}, tr)
    assert [s["url"] for s in msg["sources"]] == [
        "https://a.example", "https://b.example"]
    assert [s["n"] for s in msg["sources"]] == [3, 4]
    assert msg["search"]["hits"] == 2


def test_merge_continuation_dedupes_sources_and_keeps_n():
    last = {"role": "assistant", "content": "x",
            "sources": [{"title": "A", "url": "https://a.example", "n": 1}]}
    tr = TurnResult(content="y", sources=[
        {"title": "A", "url": "https://a.example", "n": 1, "snippet": "s"},
        {"title": "B", "url": "https://b.example", "n": 2, "snippet": "s"},
    ])
    _merge_continuation(last, tr)
    assert [s["url"] for s in last["sources"]] == [
        "https://a.example", "https://b.example"]
    assert [s["n"] for s in last["sources"]] == [1, 2]
    assert last["search"]["hits"] == 2
