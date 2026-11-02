# -*- coding: utf-8 -*-
"""S4a：直接命中与一跳邻居同池排序（命中保底，邻居争剩余名额）"""
from app import knowledge


def _it(nid):
    return {"node": {"id": nid, "title": nid}, "scope": "会话记忆", "scope_key": "session"}


def test_match_quota_is_kept_when_neighbors_score_high():
    chosen = [(10 - i, _it("m%d" % i)) for i in range(12)]
    neighbors = [(9.9, _it("nb1"), "因果"), (9.8, _it("nb2"), "因果")]
    ranked = knowledge._merge_ranked(chosen, neighbors)
    kinds = [k for _s, _i, _r, k in ranked]
    assert len(ranked) == knowledge.PICK_LIMIT
    assert kinds.count("match") >= knowledge.MATCH_KEEP
    ids = [it["node"]["id"] for _s, it, _r, _k in ranked]
    assert "nb1" in ids and "nb2" in ids


def test_low_score_neighbors_are_dropped():
    chosen = [(100 - i, _it("m%d" % i)) for i in range(12)]
    neighbors = [(1.0, _it("nb"), "因果")]
    ranked = knowledge._merge_ranked(chosen, neighbors)
    ids = [it["node"]["id"] for _s, it, _r, _k in ranked]
    assert "nb" not in ids
    assert len(ids) == knowledge.PICK_LIMIT


def test_without_neighbors_order_is_unchanged():
    chosen = [(10 - i, _it("m%d" % i)) for i in range(5)]
    ranked = knowledge._merge_ranked(chosen, [])
    assert [it["node"]["id"] for _s, it, _r, _k in ranked] == ["m0", "m1", "m2", "m3", "m4"]
    assert all(k == "match" for _s, _i, _r, k in ranked)


def test_neighbor_keeps_relation_text():
    chosen = [(10, _it("m0"))]
    neighbors = [(6.0, _it("nb"), "因果：因为量子比特会退相干")]
    ranked = knowledge._merge_ranked(chosen, neighbors)
    assert ranked[-1][3] == "neighbor"
    assert ranked[-1][2].startswith("因果：")
