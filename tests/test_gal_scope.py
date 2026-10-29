# -*- coding: utf-8 -*-
"""GAL 剧场独立会话 / 记忆隔离，以及会话记忆写入偏好修正。"""
from app import context, knowledge, mem_tool, prompt_assembly as pa, sessions


def test_session_defaults_are_isolated():
    s = {}
    sessions.ensure_session_kb_fields(s)
    assert s["gal_scope"] == "main"
    assert s["gal_use_profile"] is False
    assert s["gal_use_kb"] is False
    assert s["gal_use_rules"] is False


def test_session_use_profile_defaults_on_and_exposed():
    """会话级「读取用户画像」：默认开，且 slim_sessions 必须回传给前端。"""
    s = {"id": "s1", "messages": []}
    sessions.ensure_session_kb_fields(s)
    assert s["use_profile"] is True
    item = sessions.slim_sessions({"current": "s1", "sessions": [s]})["sessions"][0]
    assert item["use_profile"] is True

    s["use_profile"] = False
    item2 = sessions.slim_sessions({"current": "s1", "sessions": [s]})["sessions"][0]
    assert item2["use_profile"] is False


def test_session_use_memory_defaults_on_and_exposed():
    """会话级「读取会话记忆 / 知识库」：默认开，且 slim_sessions 必须回传给前端。"""
    s = {"id": "s1", "messages": []}
    sessions.ensure_session_kb_fields(s)
    assert s["use_memory"] is True
    item = sessions.slim_sessions({"current": "s1", "sessions": [s]})["sessions"][0]
    assert item["use_memory"] is True

    s["use_memory"] = False
    item2 = sessions.slim_sessions({"current": "s1", "sessions": [s]})["sessions"][0]
    assert item2["use_memory"] is False


def test_tool_prompt_can_skip_session_and_kb():
    s = {"ai_write": True, "memory": {"nodes": []}, "kb_ids": []}
    assert mem_tool.build_tool_prompt(
        s, include_profile=False, include_kb=False, include_session=False) == ""


def test_slim_sessions_exposes_gal_flags():
    s = {"id": "s1", "title": "剧场", "updated_at": 1, "messages": [],
         "gal_scope": "gal", "gal_use_profile": True,
         "gal_use_kb": False, "gal_use_rules": True}
    item = sessions.slim_sessions({"current": "s1", "sessions": [s]})["sessions"][0]
    assert item["gal_scope"] == "gal"
    assert item["gal_use_profile"] is True
    assert item["gal_use_rules"] is True


def test_slim_sessions_exposes_approval_mode():
    """审批档位必须回传，否则前端永远显示「手动审批」。"""
    s = {"id": "s1", "messages": [], "approval_mode": "ai"}
    item = sessions.slim_sessions({"current": "s1", "sessions": [s]})["sessions"][0]
    assert item["approval_mode"] == "ai"

    # 旧会话没有这个字段：按手动审批兜底
    old = {"id": "s2", "messages": []}
    item2 = sessions.slim_sessions({"current": "s2", "sessions": [old]})["sessions"][0]
    assert item2["approval_mode"] == "ask"


def test_slim_sessions_exposes_compress_marks():
    s = {"id": "s1", "messages": [], "compress_marks": [
        {"at": 1000, "index": 2, "upto": 2, "count": 2},
        {"at": 2000, "index": 4, "upto": 4, "count": 2}]}
    item = sessions.slim_sessions({"current": "s1", "sessions": [s]})["sessions"][0]
    assert [m["at"] for m in item["compress_marks"]] == [1000, 2000]


def test_slim_sessions_backfills_old_compress_mark():
    """旧会话只有 summary_upto / summary_at：自动补一条压缩标记。"""
    s = {"id": "s1", "messages": [1, 2, 3], "summary": "摘要",
         "summary_upto": 2, "summary_at": 1.5}
    item = sessions.slim_sessions({"current": "s1", "sessions": [s]})["sessions"][0]
    assert item["compress_marks"] == [{"at": 1500, "index": 2, "upto": 2, "count": 2}]


def test_trim_compress_marks_drop_and_shift():
    s = {"messages": [1, 2, 3, 4], "compress_marks": [
        {"at": 1, "index": 1, "upto": 1, "count": 1},
        {"at": 2, "index": 3, "upto": 3, "count": 2}]}
    sessions.trim_compress_marks(s, drop_after=2)
    assert [m["index"] for m in s["compress_marks"]] == [1]

    s = {"messages": [1, 2, 3, 4], "compress_marks": [
        {"at": 1, "index": 1, "upto": 1, "count": 1},
        {"at": 2, "index": 3, "upto": 3, "count": 2}]}
    sessions.trim_compress_marks(s, shift_after=1)
    assert [m["index"] for m in s["compress_marks"]] == [1, 2]


def test_new_gal_session_marks_scope(monkeypatch):
    from app.api import sessions as api_sessions
    data = {"current": "", "sessions": []}
    monkeypatch.setattr(api_sessions, "load_sessions", lambda: data)
    monkeypatch.setattr(api_sessions, "save_sessions", lambda d: None)
    monkeypatch.setattr(api_sessions.saves, "add_save", lambda *a, **k: None)

    class H:
        def _body(self):
            return {"gal_scope": "gal", "gal_use_profile": True, "gal_use_rules": True}

        def _ok_sessions(self, d):
            self.data = d

    h = H()
    api_sessions._api_session_new(h)
    s = h.data["sessions"][0]
    assert s["gal_scope"] == "gal"
    assert s["gal_enabled"] is True
    assert s["kb_ids"] == []
    assert s["gal_use_profile"] is True
    assert s["gal_use_rules"] is True


def test_gal_writable_scopes_can_exclude_global(monkeypatch):
    monkeypatch.setattr(mem_tool, "load_kb_index",
                        lambda: {"kbs": [
                            {"id": "kb_x", "name": "数学", "ai_write": True},
                            {"id": "kb_p", "name": "用户画像", "ai_write": True}]})
    monkeypatch.setattr(mem_tool, "load_profile", lambda: {"auto_learn_enabled": True})
    monkeypatch.setattr(mem_tool, "get_profile_kb", lambda: "kb_p")
    s = {"ai_write": True, "kb_ids": ["kb_x"]}
    assert [k for k, _ in mem_tool.writable_scopes(s)] == ["session", "kb_x", "kb_p"]
    assert [k for k, _ in mem_tool.writable_scopes(
        s, include_profile=False, include_kb=False)] == ["session"]


def test_gal_chat_ops_only_allow_session(monkeypatch):
    sess = {"id": "s1", "gal_scope": "gal", "memory": {"nodes": [], "edges": []}}
    monkeypatch.setattr(mem_tool, "load_sessions", lambda: {"sessions": [sess]})
    monkeypatch.setattr(mem_tool, "find_session", lambda data, sid: sess)
    monkeypatch.setattr(mem_tool, "load_kb_index", lambda: {"kbs": []})
    monkeypatch.setattr(mem_tool, "load_profile", lambda: {"auto_learn_enabled": False})
    seen = {}

    def fake_apply(sid, allowed, ops):
        seen["allowed"] = allowed
        return {}, ["作用域 kb_x 不可写"], {}

    monkeypatch.setattr(mem_tool, "apply_memory_ops", fake_apply)
    out = mem_tool.apply_chat_ops("s1", [{"scope": "kb_x", "op": "add", "title": "x"}])
    assert seen["allowed"] == []
    assert out and out["problems"]


def test_memory_prompt_prefers_session_scope():
    text = mem_tool.build_tool_prompt({"ai_write": True})
    assert "作用域选择" in text
    assert "默认写本会话记忆" in text
    assert "本会话记忆不受此限" in text
    assert "对话历史随时可能被压缩" in text


def test_profile_guidance_has_counterweight(monkeypatch):
    monkeypatch.setattr(mem_tool, "load_profile",
                        lambda: {"auto_learn_enabled": True, "learning_prompt_custom": ""})
    monkeypatch.setattr(mem_tool, "get_profile_kb", lambda: "kb_p")
    monkeypatch.setattr(mem_tool, "load_kb", lambda kid: {"nodes": []})
    text = mem_tool._profile_guidance({}, [("kb_p", "用户画像")])
    assert "不是任务工作区" in text
    assert "写本会话记忆" in text


def test_profile_read_block_is_marked_readonly(monkeypatch):
    monkeypatch.setattr(knowledge, "get_profile_kb", lambda: "kb_p")
    monkeypatch.setattr(knowledge, "load_kb", lambda kid: {"nodes": [
        {"title": "表达偏好", "category": "喜好", "content": "先给结论"}]})
    text = knowledge.profile_read_block()
    assert "只读" in text and "不要修改" in text
    assert "表达偏好" in text and "先给结论" in text


def test_gal_prompt_flags_reach_retrieval(monkeypatch):
    seen = {}

    def spy(s, q, exclude_pinned=False, skip_profile=False, include_kbs=True,
            include_session=True):
        seen.update(exclude_pinned=exclude_pinned, skip_profile=skip_profile,
                    include_kbs=include_kbs, include_session=include_session)
        return "", 0

    monkeypatch.setattr(pa, "retrieve_memory", spy)
    pa.base_system_prompt({"gal_scope": "gal"}, "q", gal_mode=True,
                          profile_manual="", tool_text="工具",
                          skip_profile=True, include_kbs=False)
    assert seen == {"exclude_pinned": True, "skip_profile": True,
                    "include_kbs": False, "include_session": True}


def test_profile_text_injected_readonly():
    text, _ = pa.base_system_prompt({}, "q", profile_text="【用户画像（只读）】X")
    assert "【用户画像（只读）】X" in text


def test_compress_prompt_keeps_task_context():
    for kw in ("主要请求与意图", "文件与代码", "错误与修复",
               "待办", "关键上下文"):
        assert kw in context.COMPRESS_PROMPT
