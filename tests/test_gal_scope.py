# -*- coding: utf-8 -*-
"""会话记忆隔离与写入偏好（原 GAL 剧场用例，GAL 下线后保留通用部分）。"""
from app import context, knowledge, mem_tool, prompt_assembly as pa, sessions


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


def test_slim_sessions_exposes_approval_mode():
    """审批档位必须回传，否则前端永远显示「手动审批」。"""
    s = {"id": "s1", "messages": [], "approval_mode": "ai"}
    item = sessions.slim_sessions({"current": "s1", "sessions": [s]})["sessions"][0]
    assert item["approval_mode"] == "ai"

    # 旧会话没有这个字段：按新的默认「自动审批」兜底
    old = {"id": "s2", "messages": []}
    item2 = sessions.slim_sessions({"current": "s2", "sessions": [old]})["sessions"][0]
    assert item2["approval_mode"] == "ai"


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


def test_writable_scopes_can_exclude_global(monkeypatch):
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


def test_memory_prompt_prefers_session_scope():
    text = mem_tool.build_tool_prompt({"ai_write": True})
    assert "作用域选择" in text
    assert "默认写本会话记忆" in text
    assert "本会话记忆不受此限" in text
    assert "对话历史随时可能被压缩" in text


def test_memory_prompt_mentions_fact_and_unlink():
    """S1/S2 之后，提示词必须让模型知道连线要写 fact、旧边可以失效 / 自动替代。"""
    text = mem_tool.build_tool_prompt({"ai_write": True})
    assert "fact 必填" in text
    assert "unlink" in text
    assert "自动替代旧边" in text


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


def test_profile_text_injected_readonly():
    text, _ = pa.base_system_prompt({}, "q", profile_text="【用户画像（只读）】X")
    assert "【用户画像（只读）】X" in text


def test_compress_prompt_keeps_task_context():
    for kw in ("主要请求与意图", "文件与代码", "错误与修复",
               "待办", "关键上下文"):
        assert kw in context.COMPRESS_PROMPT
