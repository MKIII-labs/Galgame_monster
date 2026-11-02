# -*- coding: utf-8 -*-
"""S3：记忆条目的原话锚点（source_msg = 消息下标）"""
from app import knowledge


def test_anchor_sets_source_msg_on_changed_session_node():
    s = {"memory": {"nodes": [{"id": "n1"}, {"id": "n2"}], "edges": []}}
    n = knowledge.anchor_changed({"session": {"node_add": ["n1"]}}, s, 12)
    assert n == 1
    assert s["memory"]["nodes"][0]["source_msg"] == 12
    assert "source_msg" not in s["memory"]["nodes"][1]


def test_anchor_covers_edges_and_updates():
    s = {"memory": {"nodes": [{"id": "n1"}],
                    "edges": [{"id": "e1"}, {"id": "e2"}]}}
    n = knowledge.anchor_changed(
        {"session": {"node_update": ["n1"], "edge_add": ["e1"], "edge_update": ["e2"]}},
        s, 7)
    assert n == 3
    assert s["memory"]["nodes"][0]["source_msg"] == 7
    assert all(e["source_msg"] == 7 for e in s["memory"]["edges"])


def test_anchor_writes_kb_and_saves(monkeypatch):
    kb = {"id": "kb1", "nodes": [{"id": "n1"}], "edges": [{"id": "e1"}]}
    saved = []
    monkeypatch.setattr(knowledge, "load_kb",
                        lambda kid: kb if kid == "kb1" else None)
    monkeypatch.setattr(knowledge, "save_kb", lambda x: saved.append(x["id"]))
    n = knowledge.anchor_changed({"kb1": {"node_update": ["n1"], "edge_add": ["e1"]}},
                                 None, 3)
    assert n == 2
    assert saved == ["kb1"]
    assert kb["nodes"][0]["source_msg"] == 3
    assert kb["edges"][0]["source_msg"] == 3


def test_anchor_skips_missing_kb(monkeypatch):
    monkeypatch.setattr(knowledge, "load_kb", lambda kid: None)
    monkeypatch.setattr(knowledge, "save_kb", lambda x: (_ for _ in ()).throw(AssertionError))
    assert knowledge.anchor_changed({"kb_gone": {"node_add": ["n1"]}}, None, 1) == 0


def test_anchor_ignores_bad_index():
    s = {"memory": {"nodes": [{"id": "n1"}], "edges": []}}
    assert knowledge.anchor_changed({"session": {"node_add": ["n1"]}}, s, None) == 0
    assert knowledge.anchor_changed({"session": {"node_add": ["n1"]}}, s, -1) == 0
    assert "source_msg" not in s["memory"]["nodes"][0]
