# -*- coding: utf-8 -*-
"""统一记忆：写入、检索、去重、重建索引"""
import pytest

from app import db, memory


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.configure(tmp_path / "test.db")
    yield


def test_remember_and_search():
    memory.remember("我喜欢喝美式咖啡，不加糖", type="preference", importance=0.8)
    items = memory.search("咖啡")
    assert items and "美式咖啡" in items[0]["text"]


def test_duplicate_remember_updates_instead_of_inserting():
    memory.remember("明天要交周报", type="event", importance=0.4,
                    source="manual", source_id="same")
    memory.remember("明天要交周报", type="event", importance=0.9,
                    source="manual", source_id="same")
    assert memory.stats()["total"] == 1
    items = memory.search("周报")
    assert items and abs(items[0]["importance"] - 0.9) < 1e-6


def test_forget():
    it = memory.remember("临时记忆", source="manual", source_id="x")
    assert memory.forget(it["id"]) is True
    assert memory.search("临时记忆", sources=("manual",)) == []


def test_context_block_only_manual_and_task(monkeypatch):
    memory.remember("会话级偏好：喜欢简洁回答", type="preference",
                    scope="session:s1", source="manual")
    memory.remember("知识库内容不应出现在长期记忆块", type="note",
                    scope="kb:k1", source="kb")
    block = memory.context_block({"id": "s1"}, "回答 偏好")
    assert "喜欢简洁回答" in block
    assert "知识库内容" not in block


def test_search_sources_filter():
    memory.remember("手动记忆：喜欢咖啡", source="manual")
    memory.remember("任务结果：咖啡豆采购", source="task", source_id="t1")
    manual = memory.search("咖啡", sources=("manual",))
    assert manual and all(x["source"] == "manual" for x in manual)
    both = memory.search("咖啡")
    assert {x["source"] for x in both} == {"manual", "task"}


def test_append_node_note_keeps_original():
    from app import knowledge
    node = knowledge.make_node("A", "原文")
    assert node["notes"] == []
    note = knowledge.append_node_note(node, "后来补充")
    assert note and node["notes"][0]["content"] == "后来补充"
    assert node["content"] == "原文"
    assert knowledge.append_node_note(node, "   ") is None


@pytest.mark.parametrize("raw_scope", ["scope=session", "scope:session", "scope：session", " session "])
def test_apply_memory_ops_tolerates_scope_prefix(monkeypatch, raw_scope):
    """模型偶尔会把提示词里的 scope= 前缀抄进字段值，后端要能剥掉。"""
    from app import knowledge
    sess = {"id": "s1", "memory": {"nodes": [], "edges": []}}
    monkeypatch.setattr(knowledge, "load_sessions", lambda: {"sessions": [sess]})
    monkeypatch.setattr(knowledge, "find_session", lambda data, sid: sess)
    monkeypatch.setattr(knowledge, "save_sessions", lambda data: None)

    counts, problems, changed = knowledge.apply_memory_ops(
        "s1", [], [{"scope": raw_scope, "op": "add",
                    "title": "容错条目", "content": "内容"}])
    assert problems == []
    assert counts.get("add") == 1
    assert any(n.get("title") == "容错条目" for n in sess["memory"]["nodes"])


def test_apply_memory_ops_append_note(monkeypatch):
    from app import knowledge
    sess = {"id": "s1", "memory": {
        "nodes": [{"id": "n1", "title": "A", "content": "原文", "notes": []}],
        "edges": [],
    }}
    saved = {"ok": False}
    monkeypatch.setattr(knowledge, "load_sessions", lambda: {"sessions": [sess]})
    monkeypatch.setattr(knowledge, "find_session", lambda data, sid: sess)
    monkeypatch.setattr(knowledge, "save_sessions", lambda data: saved.update(ok=True))
    counts, problems, changed = knowledge.apply_memory_ops(
        "s1", [], [{"scope": "session", "op": "append_note",
                    "id": "n1", "content": "补充"}])
    assert counts.get("append_note") == 1
    assert problems == []
    assert sess["memory"]["nodes"][0]["notes"][0]["content"] == "补充"
    assert changed["session"]["node_update"] == ["n1"]
    assert saved["ok"] is True


def test_reindex_mirrors_kb_and_session(monkeypatch):
    monkeypatch.setattr(memory, "load_kb_index",
                        lambda: {"kbs": [{"id": "k1", "name": "测试库"}]})
    monkeypatch.setattr(memory, "load_kb", lambda kb_id: {
        "id": "k1", "nodes": [{"id": "n1", "title": "项目代号",
                               "content": "北极星", "category": "项目",
                               "tags": ["代号"], "created_at": 1}]})
    monkeypatch.setattr(memory, "load_sessions", lambda: {"sessions": [{
        "id": "s1", "memory": {"nodes": [{"id": "n2", "title": "用户偏好",
                                         "content": "喜欢短句", "category": "偏好",
                                         "created_at": 1}]}}]})
    result = memory.reindex()
    assert result["mirrored"] == 2
    items = memory.search("北极星")
    assert items and items[0]["source"] == "kb"
    assert items[0]["type"] == "note"
    session_hit = memory.search("短句")[0]
    assert session_hit["scope"] == "session:s1"
    assert session_hit["type"] == "preference"


def test_add_memory_resolves_session_target(monkeypatch):
    s = {"id": "s1", "memory": {"nodes": [], "edges": []}, "kb_ids": []}
    monkeypatch.setattr(memory, "load_sessions",
                        lambda: {"current": "s1", "sessions": [s]})
    monkeypatch.setattr(memory, "find_session",
                        lambda data, sid: s if sid == "s1" else None)
    called = {}

    def fake_apply(sid, allowed, ops):
        called["ops"] = ops
        return {"add": 1}, [], {"session": {"node_add": {"n1"}}}

    monkeypatch.setattr(memory, "apply_memory_ops", fake_apply)
    monkeypatch.setattr(memory, "ensure_fresh", lambda force=False: None)
    monkeypatch.setattr(memory, "search", lambda *a, **k: [{"id": "m1"}])
    r = memory.add_memory("我喜欢咖啡", type="fact", target="auto",
                          session_id="s1")
    assert r["target"] == "session"
    assert r["node_id"] == "n1"
    assert r["source_name"] == "本会话记忆"
    assert called["ops"][0]["scope"] == "session"
    assert called["ops"][0]["title"] == "我喜欢咖啡"


def test_reindex_mirrors_notes(monkeypatch, tmp_path):
    monkeypatch.setattr(memory.noteslib, "NOTES_DIR", str(tmp_path))
    monkeypatch.setattr(memory.noteslib, "_all_notes", lambda: {
        "学习/线性代数.md": "# 线性代数\n特征值与特征向量"})
    memory.reindex()
    hit = memory.search("特征值", sources=("note",))[0]
    assert hit["type"] == "note"
    assert hit["title"] == "线性代数"
    assert hit["note_path"] == "学习/线性代数.md"
    assert hit["source_name"] == "笔记库"


def test_add_memory_splits_title_and_body(monkeypatch):
    s = {"id": "s1", "memory": {"nodes": [], "edges": []}, "kb_ids": []}
    monkeypatch.setattr(memory, "load_sessions",
                        lambda: {"current": "s1", "sessions": [s]})
    monkeypatch.setattr(memory, "find_session",
                        lambda data, sid: s if sid == "s1" else None)
    called = {}

    def fake_apply(sid, allowed, ops):
        called["ops"] = ops
        return {"add": 1}, [], {"session": {"node_add": {"n1"}}}

    monkeypatch.setattr(memory, "apply_memory_ops", fake_apply)
    monkeypatch.setattr(memory, "ensure_fresh", lambda force=False: None)
    monkeypatch.setattr(memory, "search", lambda *a, **k: [])
    memory.add_memory("周会时间\n每周五下午三点", target="session",
                      session_id="s1")
    assert called["ops"][0]["title"] == "周会时间"
    assert called["ops"][0]["content"] == "每周五下午三点"


def test_add_memory_preference_goes_to_profile(monkeypatch):
    s = {"id": "s1", "memory": {"nodes": [], "edges": []}, "kb_ids": []}
    monkeypatch.setattr(memory, "load_sessions",
                        lambda: {"current": "s1", "sessions": [s]})
    monkeypatch.setattr(memory, "find_session",
                        lambda data, sid: s if sid == "s1" else None)
    monkeypatch.setattr(memory, "get_profile_kb", lambda: "pk")
    called = {}

    def fake_apply(sid, allowed, ops):
        called["allowed"] = allowed
        called["ops"] = ops
        return {"add": 1}, [], {"pk": {"node_add": {"n2"}}}

    monkeypatch.setattr(memory, "apply_memory_ops", fake_apply)
    monkeypatch.setattr(memory, "ensure_fresh", lambda force=False: None)
    monkeypatch.setattr(memory, "search", lambda *a, **k: [])
    r = memory.add_memory("回答要简短", type="preference", target="auto",
                          session_id="s1")
    assert r["target"] == "pk"
    assert r["source_name"] == "用户画像"
    assert called["allowed"] == ["pk"]
    assert called["ops"][0]["category"] == "喜好"


def test_similar_finds_existing_session_node(monkeypatch):
    s = {"id": "s1", "kb_ids": [], "memory": {"nodes": [
        {"id": "n1", "title": "咖啡偏好", "content": "喜欢美式不加糖"},
    ], "edges": []}}
    monkeypatch.setattr(memory, "load_sessions",
                        lambda: {"current": "s1", "sessions": [s]})
    monkeypatch.setattr(memory, "find_session",
                        lambda data, sid: s if sid == "s1" else None)
    hits = memory.similar("咖啡 偏好", target="session", session_id="s1")
    assert hits and hits[0]["id"] == "n1"


def test_ensure_fresh_only_rebuilds_when_signature_changes(monkeypatch):
    state = {"sig": "a", "calls": 0}
    memory._save_sig("a")
    monkeypatch.setattr(memory, "_signature", lambda: state["sig"])

    def fake_reindex():
        state["calls"] += 1
        return {"mirrored": 0, "total": 0}

    monkeypatch.setattr(memory, "reindex", fake_reindex)
    assert memory.ensure_fresh() is False
    state["sig"] = "b"
    assert memory.ensure_fresh() is True
    assert state["calls"] == 1
