# -*- coding: utf-8 -*-
"""研究评测：评测集结构 + 检索-执行交错的机检指标。"""
from app import research_eval


def _tool(name, result="", args=None):
    return {"tool": name, "args": args or {}, "result": result}


def _search_result(*urls, start=1):
    lines = ["【web_search】"]
    for i, u in enumerate(urls, start):
        lines.append("[%d] 来源 %d" % (i, i))
        lines.append("URL: " + u)
    return "\n".join(lines)


def _msgs(tools, text=""):
    return [{"role": "assistant", "content": text, "tools": tools}]


def test_eval_set_is_valid():
    data = research_eval.load_tasks()
    assert research_eval.validate(data) == []
    assert len(data["tasks"]) >= 10


def test_only_search_at_start_fails_replicate():
    task = {"id": "t", "kind": "replicate", "must_search": 2,
            "key_facts": ["合成表", "红石延迟"], "fail_signals": []}
    msgs = _msgs([
        _tool("web_search", _search_result("https://a", "https://b")),
        _tool("write_file", "已写入 a.py"),
        _tool("edit_file", "已修改 b.py"),
    ], "实现了合成表，但还没写红石延迟。")
    r = research_eval.evaluate(task, msgs)
    assert r["metrics"]["search_calls"] == 1
    assert r["metrics"]["search_after_write"] is False
    assert r["score"]["interleave_ok"] is False
    assert r["score"]["pass"] is False


def test_interleaved_search_passes():
    task = {"id": "t", "kind": "replicate", "must_search": 2,
            "key_facts": ["合成表", "红石延迟"], "fail_signals": []}
    msgs = _msgs([
        _tool("web_search", _search_result("https://a")),
        _tool("write_file", "已写入 a.py"),
        _tool("web_search", _search_result("https://b", start=2)),
        _tool("edit_file", "已修改 b.py"),
    ], "合成表与红石延迟都已实现，见 [1] 和 [2]。")
    r = research_eval.evaluate(task, msgs)
    assert r["metrics"]["search_calls"] == 2
    assert r["metrics"]["search_after_write"] is True
    assert r["score"]["interleave_ok"] is True
    assert r["score"]["grounded"] is True
    assert r["score"]["pass"] is True


def test_research_kind_does_not_require_interleave():
    task = {"id": "t", "kind": "research", "must_search": 1,
            "key_facts": ["中继器"], "fail_signals": []}
    msgs = _msgs([
        _tool("web_search", _search_result("https://a")),
    ], "中继器默认 1 刻，见 [1]。")
    r = research_eval.evaluate(task, msgs)
    assert r["score"]["interleave_ok"] is True
    assert r["score"]["pass"] is True


def test_bad_citations_are_flagged():
    task = {"id": "t", "kind": "research", "must_search": 1,
            "key_facts": ["x"], "fail_signals": []}
    msgs = _msgs([
        _tool("web_search", _search_result("https://a")),
    ], "结论见 [1] 和 [9]。")
    r = research_eval.evaluate(task, msgs)
    assert r["metrics"]["bad_citations"] == [9]
    assert r["score"]["pass"] is False
