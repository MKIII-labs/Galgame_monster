# -*- coding: utf-8 -*-
"""知识库：规则常驻注入 与 检索排除 的两态行为。
防回归：本会话真实踩过「把规则型节点从检索里排除，但替代注入只在 GAL 生效」→ 非 GAL 记忆凭空消失。"""
import pytest

from app import knowledge

FAKE_KB = {
    "id": "kb_test",
    "name": "测试库",
    "nodes": [
        {"id": "n1", "title": "光幕规则", "content": "凭空降临，带倒计时。", "category": "规则"},
        {"id": "n2", "title": "某个人物", "content": "性格与口癖。", "category": "人物"},
    ],
    "edges": [],
    "graph": {},
}


@pytest.fixture
def sess(monkeypatch):
    monkeypatch.setattr(knowledge, "load_kb", lambda kid: FAKE_KB if kid == "kb_test" else None)
    monkeypatch.setattr(knowledge, "get_profile_kb", lambda: "kb_profile")
    return {"kb_ids": ["kb_test"], "memory": {"nodes": []}}


def test_pinned_block_only_takes_rule_categories(sess):
    txt, n = knowledge.pinned_block(sess)
    assert n == 1
    assert "光幕规则" in txt
    assert "某个人物" not in txt          # 人物类不该常驻
    assert "硬性规则" in txt              # 口气必须是硬约束，不是「供参考」


def test_pinned_block_empty_without_kb():
    txt, n = knowledge.pinned_block({"kb_ids": [], "memory": {"nodes": []}})
    assert txt == "" and n == 0


def test_retrieve_memory_two_states(sess):
    # 非 GAL（exclude_pinned=False）：规则型节点照常可被检索到 —— 不能凭空消失
    blk, _ = knowledge.retrieve_memory(sess, "光幕", exclude_pinned=False)
    assert "光幕规则" in blk
    # GAL（exclude_pinned=True）：由 pinned_block 常驻接管，这里跳过
    blk2, _ = knowledge.retrieve_memory(sess, "光幕", exclude_pinned=True)
    assert "光幕规则" not in blk2


def test_world_text_covers_all_nodes(sess):
    w = knowledge.world_text(sess)
    assert "光幕规则" in w and "某个人物" in w
    assert w.count("\n") >= 1


def test_world_text_respects_budget(sess):
    assert knowledge.world_text(sess, max_chars=10) != knowledge.world_text(sess)


def test_new_kb_has_families_and_stable_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "KB_DIR", str(tmp_path))
    monkeypatch.setattr(knowledge, "KB_INDEX", str(tmp_path / "index.json"))
    kb = knowledge.new_kb("测试库")
    assert kb["families"] == []
    assert kb["layout_mode"] == "stable"
    loaded = knowledge.load_kb(kb["id"])
    assert loaded["families"] == []
    assert loaded["layout_mode"] == "stable"
    assert loaded["layout_version"] == 1
