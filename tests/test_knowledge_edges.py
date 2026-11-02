# -*- coding: utf-8 -*-
"""知识库关系演进：S1（连线必须写 fact + 关系词表收敛）与 S2（旧结论标失效不删）"""
import pytest

from app import knowledge


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch):
    """测试不读真实的「用户画像」库，也不走向量模型"""
    monkeypatch.setattr(knowledge, "get_profile_kb", lambda: "")
    monkeypatch.setattr(knowledge, "_vec_scores", lambda items, query: None)


@pytest.fixture
def sess(monkeypatch):
    s = {"id": "s1", "memory": {"nodes": [], "edges": []}, "kb_ids": []}
    monkeypatch.setattr(knowledge, "load_sessions", lambda: {"sessions": [s]})
    monkeypatch.setattr(knowledge, "save_sessions", lambda data: None)
    return s


def _add(sess, *titles):
    for t in titles:
        sess["memory"]["nodes"].append(knowledge.make_node(t))
    return sess


def _link(a, b, rel="cause", fact="", **kw):
    op = {"scope": "session", "op": "link", "from": a, "to": b, "relation": rel}
    if fact:
        op["fact"] = fact
    op.update(kw)
    return op


# ------------------------------------------------------------
# 关系词表
# ------------------------------------------------------------
def test_relations_dropped_sequence_and_turn():
    assert "sequence" not in knowledge.RELATION_CODES
    assert "turn" not in knowledge.RELATION_CODES
    # 老数据仍要能显示原来的中文名
    assert knowledge.relation_label("sequence") == "顺承"
    assert knowledge.relation_label("turn") == "转折"


def test_normalize_relation_maps_legacy_to_related():
    assert knowledge.normalize_relation("顺承") == "related"
    assert knowledge.normalize_relation("sequence") == "related"
    assert knowledge.normalize_relation("转折") == "related"


# ------------------------------------------------------------
# S1：fact 必填 + (from,to,relation) 唯一
# ------------------------------------------------------------
def test_link_without_fact_is_rejected(sess):
    _add(sess, "A", "B")
    counts, problems, _changed = knowledge.apply_memory_ops("s1", [], [_link("A", "B")])
    assert not counts.get("link")
    assert sess["memory"]["edges"] == []
    assert any("fact" in p for p in problems)


def test_link_with_fact_writes_edge(sess):
    _add(sess, "A", "B")
    counts, problems, _changed = knowledge.apply_memory_ops(
        "s1", [], [_link("A", "B", fact="A 导致 B")])
    assert counts.get("link") == 1
    assert not problems
    edge = sess["memory"]["edges"][0]
    assert edge["fact"] == "A 导致 B"
    assert edge["relation"] == "cause"
    assert edge.get("valid_at")
    assert not edge.get("invalid_at")


def test_same_edge_same_fact_is_idempotent(sess):
    _add(sess, "A", "B")
    op = _link("A", "B", fact="同一句依据")
    knowledge.apply_memory_ops("s1", [], [op])
    knowledge.apply_memory_ops("s1", [], [op])
    assert len(sess["memory"]["edges"]) == 1


def test_legacy_edge_without_fact_gets_filled(sess):
    """老边没有 fact：新陈述补进去，不产生第二条边、也不标失效"""
    _add(sess, "A", "B")
    a, b = sess["memory"]["nodes"]
    old = {"id": "e_old", "from": a["id"], "to": b["id"], "relation": "cause",
           "label": "因果", "directed": True, "type": "relation"}
    sess["memory"]["edges"].append(old)
    knowledge.apply_memory_ops("s1", [], [_link("A", "B", fact="补上的依据")])
    assert len(sess["memory"]["edges"]) == 1
    assert old["fact"] == "补上的依据"
    assert not old.get("invalid_at")


# ------------------------------------------------------------
# S2：新陈述替代旧陈述
# ------------------------------------------------------------
def test_new_fact_invalidates_old_edge(sess):
    _add(sess, "A", "B")
    knowledge.apply_memory_ops("s1", [], [_link("A", "B", fact="旧结论")])
    knowledge.apply_memory_ops("s1", [], [_link("A", "B", fact="新结论")])
    edges = sess["memory"]["edges"]
    assert len(edges) == 2                      # 旧的没删，只是失效
    old, new = edges
    assert old["fact"] == "旧结论" and old.get("invalid_at")
    assert new["fact"] == "新结论" and not new.get("invalid_at")


def test_unlink_marks_edge_invalid(sess):
    _add(sess, "A", "B")
    knowledge.apply_memory_ops("s1", [], [_link("A", "B", fact="一条依据")])
    counts, problems, _c = knowledge.apply_memory_ops(
        "s1", [], [{"scope": "session", "op": "unlink", "from": "A", "to": "B"}])
    assert counts.get("unlink") == 1
    assert not problems
    assert sess["memory"]["edges"][0].get("invalid_at")


def test_unlink_missing_edge_reports_problem(sess):
    _add(sess, "A", "B")
    counts, problems, _c = knowledge.apply_memory_ops(
        "s1", [], [{"scope": "session", "op": "unlink", "from": "A", "to": "B"}])
    assert not counts.get("unlink")
    assert problems


# ------------------------------------------------------------
# 检索：失效边不参与邻居扩展，有效边把 fact 一起带进注入
# ------------------------------------------------------------
def _retrieve_text(sess, edge):
    sess["memory"]["nodes"] = [
        {"id": "n1", "title": "量子计算", "content": "量子 比特", "category": "其他"},
        {"id": "n2", "title": "纠错方案", "content": "表面码", "category": "其他"},
    ]
    sess["memory"]["edges"] = [edge]
    text, _n = knowledge.retrieve_memory(
        sess, "量子", include_kbs=False, include_session=True,
        exclude_pinned=True, skip_profile=True)
    return text


def test_valid_edge_expands_neighbor_with_fact(sess):
    edge = {"id": "e1", "from": "n1", "to": "n2", "relation": "cause",
            "label": "因果", "directed": True, "fact": "因为量子比特会退相干"}
    text = _retrieve_text(sess, edge)
    assert "表面码" in text
    assert "因为量子比特会退相干" in text


def test_invalid_edge_is_not_used_for_expansion(sess):
    edge = {"id": "e1", "from": "n1", "to": "n2", "relation": "cause",
            "label": "因果", "directed": True, "fact": "旧依据",
            "invalid_at": 1.0}
    text = _retrieve_text(sess, edge)
    assert "表面码" not in text
