# -*- coding: utf-8 -*-
"""知识库检索：分词、字段权重、IDF、长度归一化、预算、邻居、别名、导出"""
import pytest

from app import knowledge
from app.textmatch import tokens as tm_tokens


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch):
    """测试不读真实的「用户画像」库"""
    monkeypatch.setattr(knowledge, "get_profile_kb", lambda: "")
    # 这些用例断言的是纯关键词排序；向量可用与否不该改变它们的结果
    monkeypatch.setattr(knowledge, "_vec_scores", lambda items, query: None)


def _node(title, content="", category="其他", tags=None, aliases=None, nid=None):
    n = {"id": nid or ("n_" + title), "title": title, "content": content,
         "category": category, "tags": tags or [], "aliases": aliases or []}
    return n


def _item(n):
    return {"node": n, "scope": "会话记忆", "scope_key": "session"}


def test_textmatch_uses_bigrams_and_stopwords():
    toks = tm_tokens("我的知识库")
    assert "知识" in toks and "识库" in toks
    assert "的" not in toks


def test_title_and_alias_outrank_body():
    q = set(knowledge.tokenize("deadline"))
    title_node = _node("项目 deadline")
    alias_node = _node("项目排期", aliases=["deadline"])
    body_node = _node("其他", content="项目 deadline 在周五")
    assert knowledge.score_node(title_node, q) > knowledge.score_node(body_node, q)
    assert knowledge.score_node(alias_node, q) > knowledge.score_node(body_node, q)


def test_idf_prefers_rare_token():
    items = [_item(_node("项目 %d" % i, "项目 进度")) for i in range(10)]
    items.append(_item(_node("量子计算", "量子 比特")))
    chosen, q, triggered = knowledge._rank_items(items, "项目 量子")
    assert chosen[0][1]["node"]["title"] == "量子计算"
    assert triggered is False


def test_length_normalization():
    q = set(knowledge.tokenize("量子"))
    short = _node("A", "量子")
    long = _node("B", "量子" + "无关内容" * 300)
    assert knowledge.score_node(short, q) > knowledge.score_node(long, q)


def test_retrieve_can_skip_session_memory():
    s = {"memory": {"nodes": [_node("量子计算", "量子 比特")], "edges": []}}
    text, n = knowledge.retrieve_memory(
        s, "量子", include_kbs=False, include_session=False)
    assert text == "" and n == 0

    items, _e, _n = knowledge._collect_items(
        s, include_kbs=False, include_session=False)
    assert items == []
    items2, _e2, _n2 = knowledge._collect_items(
        s, include_kbs=False, include_session=True)
    assert len(items2) == 1


def test_retrieve_budget_limits_lines():
    nodes = [_node("量子 %d" % i, "量子" * 200) for i in range(20)]
    s = {"memory": {"nodes": nodes, "edges": []}}
    text, count = knowledge.retrieve_memory(s, "量子", include_kbs=False, budget_tokens=200)
    assert count >= 1
    assert knowledge.estimate_tokens(text) < 600      # 第一条允许超一点，但不能整批塞进来


def test_neighbor_expansion_marks_relation():
    a = _node("量子计算", "量子 比特", nid="a")
    b = _node("芯片工艺", "光刻", nid="b")
    s = {"memory": {"nodes": [a, b], "edges": [
        {"id": "e1", "from": "a", "to": "b", "relation": "cause",
         "label": "因果", "directed": True}]}}
    text, _n = knowledge.retrieve_memory(s, "量子", include_kbs=False)
    assert "量子计算" in text
    assert "关联·因果" in text and "芯片工艺" in text


def test_alias_find_and_rank():
    nodes = [_node("人工智能", aliases=["AI", "机器学习"])]
    assert knowledge._find_node(nodes, title="AI", fuzzy=True) is nodes[0]
    chosen, _q, _t = knowledge._rank_items([_item(nodes[0])], "AI")
    assert chosen and chosen[0][1]["node"]["title"] == "人工智能"


def test_clean_aliases():
    assert knowledge._clean_aliases("a, b，a、c") == ["a", "b", "c"]
    assert knowledge._clean_aliases("x" * 100)[0] == "x" * 40
    assert len(knowledge._clean_aliases([str(i) for i in range(20)])) == 8
    assert knowledge._clean_aliases(None) == []


def test_recall_detail_shape():
    s = {"memory": {"nodes": [_node("量子计算", "量子 比特")], "edges": []}}
    d = knowledge.recall_detail(s, "量子", include_kbs=False)
    assert d["used_model"] is False
    assert d["candidates"] == 1
    assert d["items"][0]["title"] == "量子计算"
    assert d["items"][0]["kind"] == "match"
    assert "量子" in d["tokens"]


def test_recall_detail_expands_short_query():
    s = {"messages": [{"role": "user", "content": "上次说的量子比特"},
                      {"role": "user", "content": "继续"}],
         "memory": {"nodes": [], "edges": []}}
    d = knowledge.recall_detail(s, "继续", include_kbs=False)
    assert "量子比特" in d["query"]


def test_export_markdown():
    kb = {"name": "测试库", "nodes": [
        _node("量子计算", "量子 比特", category="概念", tags=["物理"], aliases=["QC"], nid="a"),
        _node("芯片工艺", "光刻", nid="b")],
        "edges": [{"from": "a", "to": "b", "relation": "cause", "label": "因果",
                   "directed": True}]}
    md = knowledge_api_markdown(kb)
    assert md.startswith("---")
    assert 'title: "测试库"' in md
    assert "## 量子计算" in md
    assert "分类：概念" in md and "别名：QC" in md
    # 节点内是链接，陈述统一放在末尾「事实」区
    assert "- 因果 → [[芯片工艺]]" in md
    assert "## 事实" in md
    assert "- [[量子计算]] --因果--> [[芯片工艺]]" in md


def knowledge_api_markdown(kb):
    """避免在测试里直接 import api 层（其函数缩进风格特殊，但逻辑是纯函数）"""
    from app.api.kb import _kb_markdown
    return _kb_markdown(kb)


def test_vec_recalls_when_keywords_miss(monkeypatch):
    """关键词完全没命中、但向量像的条目要被召回（同义 / 换说法）"""
    items = [_item(_node("甲", "今天天气不错")), _item(_node("乙", "完全不同的字面"))]
    ids = [it["node"]["id"] for it in items]
    monkeypatch.setattr(knowledge, "_vec_scores",
                        lambda its, q: {ids[0]: 0.90, ids[1]: 0.10})
    chosen, _q, _t = knowledge._rank_items(items, "某个查询词")
    assert [c[1]["node"]["title"] for c in chosen] == ["甲"]


def test_vec_keeps_keyword_hit_first(monkeypatch):
    """关键词命中的条目要压过「向量略高但字面无关」的条目"""
    items = [_item(_node("甲", "量子 比特")), _item(_node("乙", "无关内容"))]
    ids = [it["node"]["id"] for it in items]
    monkeypatch.setattr(knowledge, "_vec_scores",
                        lambda its, q: {ids[0]: 0.20, ids[1]: 0.40})
    chosen, _q, _t = knowledge._rank_items(items, "量子")
    assert chosen[0][1]["node"]["title"] == "甲"


def test_vec_low_score_filtered(monkeypatch):
    """关键词 0 分、向量也低于阈值 → 不召回（避免噪声条目混进注入）"""
    items = [_item(_node("甲", "无关内容"))]
    monkeypatch.setattr(knowledge, "_vec_scores",
                        lambda its, q: {items[0]["node"]["id"]: 0.10})
    chosen, _q, _t = knowledge._rank_items(items, "zzz")
    assert chosen == []


def test_profile_injection_follows_session_switch(monkeypatch):
    """画像注入是会话级开关：默认读，use_profile=False 的会话不读。"""
    monkeypatch.setattr(knowledge, "get_profile_kb", lambda: "kb_p")
    monkeypatch.setattr(knowledge, "load_kb", lambda kid: {
        "id": "kb_p", "name": "用户画像",
        "nodes": [{"id": "p1", "title": "偏好简洁", "category": "交流风格"}]})
    s = {"memory": {"nodes": [], "edges": []}}
    text, n = knowledge.retrieve_memory(s, "你好", include_kbs=False)
    assert "【用户画像】" in text
    assert n == 1

    off = dict(s, use_profile=False)
    text2, n2 = knowledge.retrieve_memory(off, "你好", include_kbs=False)
    assert "【用户画像】" not in text2
    assert n2 == 0
