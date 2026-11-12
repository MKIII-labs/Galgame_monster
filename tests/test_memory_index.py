# -*- coding: utf-8 -*-
"""L3 有界记忆索引（MEMORY.md 式）与可配置的记忆注入预算。"""
import pytest

from app import knowledge


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    knowledge._MEM_INDEX_CACHE.clear()
    monkeypatch.setattr(knowledge, "light_chat_is_local", lambda cfg=None: False)
    monkeypatch.setattr(knowledge, "load_config",
                        lambda: {"memory_index_enabled": True,
                                 "memory_index_max_lines": 200,
                                 "memory_index_max_bytes": 25000})
    monkeypatch.setattr(knowledge, "load_kb_index",
                        lambda: {"kbs": [{"id": "pk", "name": "用户画像"},
                                         {"id": "kb1", "name": "世界设定"}]})

    def fake_kb(kid):
        if kid == "pk":
            return {"id": "pk", "name": "用户画像", "nodes": [
                {"id": "p1", "title": "表达偏好", "content": "喜欢简洁",
                 "category": "偏好", "updated_at": 10}]}
        if kid == "kb1":
            return {"id": "kb1", "name": "世界设定", "nodes": [
                {"id": "n1", "title": "世界规则", "content": "第一条规则\n第二条",
                 "category": "规则", "updated_at": 20},
                {"id": "n2", "title": "地理", "content": "北境寒冷",
                 "category": "设定", "updated_at": 30}]}
        return None

    monkeypatch.setattr(knowledge, "load_kb", fake_kb)
    yield
    knowledge._MEM_INDEX_CACHE.clear()


def _session():
    return {"id": "s1", "kb_ids": ["kb1"],
            "memory": {"nodes": [
                {"id": "n3", "title": "会话条目", "content": "会话钩子",
                 "category": "事件", "updated_at": 5}]}}


def test_index_renders_titles_and_hooks():
    out = knowledge.memory_index_block(_session())
    assert out.startswith("【记忆索引】")
    assert "[世界设定·规则] 世界规则 — 第一条规则" in out
    assert "[世界设定·设定] 地理 — 北境寒冷" in out
    assert "[会话记忆·事件] 会话条目 — 会话钩子" in out
    assert "[用户画像·偏好] 表达偏好 — 喜欢简洁" in out


def test_index_can_exclude_profile():
    out = knowledge.memory_index_block(_session(), include_profile=False)
    assert "用户画像" not in out
    assert "世界设定" in out


def test_index_is_cached_until_memory_changes():
    s = _session()
    first = knowledge.memory_index_block(s)
    second = knowledge.memory_index_block(s)
    assert second == first                     # 记忆没变：直接复用

    s["memory"]["nodes"][0]["content"] = "新的钩子内容更长"
    s["memory"]["nodes"][0]["updated_at"] = 99
    third = knowledge.memory_index_block(s)
    assert "新的钩子内容更长" in third


def test_index_skips_history_summary_nodes():
    s = {"id": "s2", "memory": {"nodes": [
        {"id": "h1", "title": "历史摘要A", "content": "压缩要点",
         "category": "历史摘要", "updated_at": 2},
        {"id": "n1", "title": "正常事件", "content": "钩子",
         "category": "事件", "updated_at": 1}]}}
    out = knowledge.memory_index_block(s, include_profile=False)
    assert "历史摘要A" not in out
    assert "正常事件" in out


def test_index_respects_line_cap():
    s = _session()
    s["memory"]["nodes"] = [
        {"id": "x%d" % i, "title": "条目%d" % i, "content": "钩子%d" % i,
         "category": "其他", "updated_at": i} for i in range(10)]
    out = knowledge.memory_index_block(s, max_lines=2)
    assert "已按行数 / 字节上限截断" in out
    assert out.count("\n- ") == 2


def test_index_disabled_and_local_model_skipped(monkeypatch):
    monkeypatch.setattr(knowledge, "load_config",
                        lambda: {"memory_index_enabled": False})
    assert knowledge.memory_index_block(_session()) == ""
    monkeypatch.setattr(knowledge, "load_config",
                        lambda: {"memory_index_enabled": True})
    monkeypatch.setattr(knowledge, "light_chat_is_local", lambda cfg=None: True)
    assert knowledge.memory_index_block(_session()) == ""


def test_inject_budget_is_configurable(monkeypatch):
    monkeypatch.setattr(knowledge, "load_config",
                        lambda: {"memory_inject_tokens": 5000})
    assert knowledge.inject_budget() == 5000
    monkeypatch.setattr(knowledge, "load_config",
                        lambda: {"memory_inject_tokens": 1})
    assert knowledge.inject_budget() == 300      # 下限
    monkeypatch.setattr(knowledge, "load_config",
                        lambda: {"memory_inject_tokens": 999999})
    assert knowledge.inject_budget() == 50000    # 上限
