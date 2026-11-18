# -*- coding: utf-8 -*-
"""resume：断点摘要内容 + 启动时把 _progress 快照转成「可继续任务」。"""
from app import sessions
from app.api.chat import _resume_brief


def test_resume_brief_lists_tools_and_plan():
    msg = {"role": "assistant", "content": "写到一半",
           "tools": [{"tool": "read_file", "args": {"path": "src/main.js"},
                      "result": "123 行", "ok": True},
                     {"tool": "edit_file", "args": {"path": "src/main.js"},
                      "result": "失败", "ok": False}],
           "plan": {"steps": [{"text": "修渲染", "status": "doing"},
                              {"text": "跑测试", "status": "pending"}]}}
    note = _resume_brief(msg)
    assert "read_file" in note and "src/main.js" in note
    assert "（失败）" in note
    assert "[doing]" in note and "跑测试" in note
    assert "不要重复" in note


def test_resume_brief_empty_tools():
    note = _resume_brief({"role": "assistant", "content": "x"})
    assert "没有记录到工具操作" in note


def test_mark_resume_pending():
    data = {"sessions": [{"id": "s1", "messages": [
        {"role": "user", "content": "做任务"},
        {"role": "assistant", "content": "半截", "_progress": True,
         "tools": [{"tool": "read_file"}]}]}]}
    assert sessions._mark_resume_pending(data) is True
    m = data["sessions"][0]["messages"][-1]
    assert "_progress" not in m
    assert m["interrupted"] is True
    assert m["end_reason"]["action"] == "resume"
    assert data["sessions"][0]["resume_pending"] is True
    # 已经转过一次：不应再判为需要写盘
    assert sessions._mark_resume_pending(data) is False


def test_save_progress_does_not_mark_interrupted(monkeypatch):
    """运行中的进度快照不能标 interrupted。

    现场：后台任务还在跑，刷新/重开窗口后最后一条是 _progress 快照；
    旧代码把它标成 interrupted，前端就显示「继续输出」，看起来像输出停了。
    真正的中断只由进程重启后的 _mark_resume_pending 标记。
    """
    from app.api import chat

    data = {"sessions": [{"id": "s1", "messages": [
        {"role": "user", "content": "做任务"},
        {"role": "assistant", "content": "半截", "_progress": True,
         "interrupted": True, "end_reason": {"kind": "resume"}}]}]}
    monkeypatch.setattr(chat, "load_sessions", lambda: data)
    monkeypatch.setattr(chat, "find_session",
                        lambda d, sid: d["sessions"][0] if sid == "s1" else None)
    monkeypatch.setattr(chat, "_persist_session", lambda s: None)

    chat._save_progress("s1", "半截更多", [{"tool": "read_file"}], {"steps": []})
    m = data["sessions"][0]["messages"][-1]
    assert m["_progress"] is True
    assert "interrupted" not in m
    assert "end_reason" not in m

    # 进程重启时才转成中断
    assert sessions._mark_resume_pending(data) is True
    assert m["interrupted"] is True
    assert m["end_reason"]["kind"] == "resume"


def test_mark_resume_pending_skips_live_stream():
    """有活跃流的会话不能被标成中断：否则生成中的进度快照会被当成孤儿。"""
    data = {"sessions": [{"id": "s1", "messages": [
        {"role": "user", "content": "做任务"},
        {"role": "assistant", "content": "半截", "_progress": True}]}]}
    assert sessions._mark_resume_pending(data, live_ids={"s1"}) is False
    assert data["sessions"][0]["messages"][-1]["_progress"] is True
    assert sessions._mark_resume_pending(data) is True


def test_save_progress_merges_stripped_resume_snapshot(monkeypatch):
    """被误标成 resume 的进度快照，下一次落库必须就地续写，不能新建第二条。

    现场：408 字那条被标中断后，流式循环新建了 4498 字那条，于是同一轮回答
    出现两段重复气泡。
    """
    from app.api import chat

    data = {"sessions": [{"id": "s1", "messages": [
        {"role": "user", "content": "做任务"},
        {"role": "assistant", "content": "第一段", "interrupted": True,
         "end_reason": {"kind": "resume", "action": "resume"}}]}]}
    monkeypatch.setattr(chat, "load_sessions", lambda: data)
    monkeypatch.setattr(chat, "find_session",
                        lambda d, sid: d["sessions"][0] if sid == "s1" else None)
    monkeypatch.setattr(chat, "_persist_session", lambda s: None)

    chat._save_progress("s1", "第一段第二段", [{"tool": "read_file"}], {"steps": []})
    msgs = data["sessions"][0]["messages"]
    assert len(msgs) == 2
    last = msgs[-1]
    assert last["content"] == "第一段第二段"
    assert last["_progress"] is True
    assert "interrupted" not in last
    assert "end_reason" not in last


def test_collapse_resume_duplicates():
    """断点误标留下的 408 / 4498 / 11822 三条前缀消息，合并成最后一条。"""
    data = {"sessions": [{"id": "s1", "messages": [
        {"role": "user", "content": "复盘"},
        {"role": "assistant", "content": "a" * 408, "interrupted": True,
         "end_reason": {"kind": "resume"}},
        {"role": "assistant", "content": "a" * 408 + "b" * 4090, "interrupted": True,
         "end_reason": {"kind": "resume"}},
        {"role": "assistant", "content": "a" * 408 + "b" * 4090 + "c" * 7324,
         "end_reason": {"kind": "done"}},
    ]}]}
    assert sessions._collapse_resume_duplicates(data) is True
    msgs = data["sessions"][0]["messages"]
    assert len(msgs) == 2
    assert msgs[-1]["content"].endswith("c" * 100)
    # 完全相同的一对（同一次输出被存了两遍）也要合并
    same = {"sessions": [{"id": "s3", "messages": [
        {"role": "assistant", "content": "同一段", "interrupted": True,
         "end_reason": {"kind": "resume"}},
        {"role": "assistant", "content": "同一段"}]}]}
    assert sessions._collapse_resume_duplicates(same) is True
    assert len(same["sessions"][0]["messages"]) == 1
    # 无关的相邻 assistant 不能被合并
    normal = {"sessions": [{"id": "s2", "messages": [
        {"role": "assistant", "content": "第一条"},
        {"role": "assistant", "content": "第二条（不同内容）"}]}]}
    assert sessions._collapse_resume_duplicates(normal) is False
    assert len(normal["sessions"][0]["messages"]) == 2


def test_save_progress_inplace_for_continue(monkeypatch):
    """续写（继续输出 / 断点续跑）：原地更新目标消息，不再新增第二条输出块。

    现场：续跑后进度快照被写成一条新消息，下次请求开始时又把它降级成普通消息，
    结果同一轮输出被拆成「旧回答 + 只含新段落的块」，计划卡也停在旧块。
    """
    from app.api import chat

    data = {"sessions": [{"id": "s1", "messages": [
        {"role": "user", "content": "做任务"},
        {"role": "assistant", "content": "原来写好的内容"},
    ]}]}
    monkeypatch.setattr(chat, "load_sessions", lambda: data)
    monkeypatch.setattr(chat, "find_session",
                        lambda d, sid: d["sessions"][0] if sid == "s1" else None)
    monkeypatch.setattr(chat, "_persist_session", lambda s: None)

    chat._save_progress("s1", "新的续写内容", [{"tool": "read_file"}],
                        {"steps": [{"text": "收尾", "status": "doing"}]},
                        inplace_base="原来写好的内容")
    msgs = data["sessions"][0]["messages"]
    assert len(msgs) == 2                          # 没有多出一条输出块
    last = msgs[-1]
    assert last["content"] == "原来写好的内容新的续写内容"
    assert last["_cont_base"] == "原来写好的内容"
    assert last["_progress"] is True
    assert last["plan"]["steps"][0]["status"] == "doing"

    # 下一次快照仍以原文为基：内容替换而不是叠加，避免重复拼接
    chat._save_progress("s1", "又写了一点", None, None,
                        inplace_base="原来写好的内容")
    assert msgs[-1]["content"] == "原来写好的内容又写了一点"
    assert msgs[-1]["_cont_base"] == "原来写好的内容"
    assert len(msgs) == 2


def test_save_progress_continue_merges_tools(monkeypatch):
    """续写快照不能把前几轮的工具覆盖掉，也不能把本次工具重复写两遍。

    现场（前端设计风格图谱 #27）：连续两次「继续输出」后，消息里 156 个工具块
    只对应 70 条 tools —— 32 个块下标越界、34 条工具记录 trace_id 重复。
    """
    from app.api import chat

    data = {"sessions": [{"id": "s1", "messages": [
        {"role": "user", "content": "做任务"},
        {"role": "assistant", "content": "原文",
         "tools": [{"tool": "read_file"}, {"tool": "edit_file"}]},
    ]}]}
    monkeypatch.setattr(chat, "load_sessions", lambda: data)
    monkeypatch.setattr(chat, "find_session",
                        lambda d, sid: d["sessions"][0] if sid == "s1" else None)
    monkeypatch.setattr(chat, "_persist_session", lambda s: None)

    chat._save_progress("s1", "新增一", [{"tool": "run_command"}], None,
                        inplace_base="原文", tools_base=2)
    last = data["sessions"][0]["messages"][-1]
    assert [t["tool"] for t in last["tools"]] == [
        "read_file", "edit_file", "run_command"]

    # 第二次快照：上次已写进去的 run_command 不能在本次再追加一遍
    chat._save_progress("s1", "新增二",
                        [{"tool": "run_command"}, {"tool": "render_check"}], None,
                        inplace_base="原文", tools_base=2)
    assert [t["tool"] for t in last["tools"]] == [
        "read_file", "edit_file", "run_command", "render_check"]


def test_merge_tools_replaces_only_this_run():
    """收尾合并：base 之前的旧工具原样保留，之后用本次完整 tool_log 覆盖。"""
    from app.api import chat

    old = [{"tool": "a"}, {"tool": "b"}, {"tool": "c"}]
    assert chat._merge_tools(old, [{"tool": "c"}, {"tool": "d"}], 2) == [
        {"tool": "a"}, {"tool": "b"}, {"tool": "c"}, {"tool": "d"}]


def test_apply_progress_snapshot_inplace():
    """进度快照的纯逻辑：拼正文、合并工具、清中断标记、带计划。"""
    from app.api import chat

    msg = {"role": "assistant", "content": "原文", "tools": [{"tool": "a"}],
           "interrupted": True, "end_reason": {"kind": "stopped"},
           "plan": {"steps": [{"text": "旧", "status": "doing"}]}}
    out = chat._apply_progress_snapshot(
        msg, "新增", [{"tool": "b"}],
        {"steps": [{"text": "收尾", "status": "doing"}]},
        inplace_base="原文", tools_base=1)
    assert out is msg
    assert out["content"] == "原文新增"
    assert out["_cont_base"] == "原文"
    assert out["_progress"] is True
    assert [t["tool"] for t in out["tools"]] == ["a", "b"]
    assert "interrupted" not in out and "end_reason" not in out
    assert out["plan"]["steps"][0]["status"] == "doing"


def test_apply_progress_snapshot_normal_replaces_tools():
    """非续写快照：正文与工具整份替换，不做合并。"""
    from app.api import chat

    msg = {"role": "assistant", "content": "旧", "tools": [{"tool": "a"}]}
    out = chat._apply_progress_snapshot(msg, "新", [{"tool": "b"}])
    assert out["content"] == "新"
    assert [t["tool"] for t in out["tools"]] == ["b"]


def test_merge_usage_adds_tokens():
    """续写用量累加：token 字段相加，模型名取有值的那次。"""
    from app.api import chat

    prev = {"prompt": 10, "completion": 5, "total": 15, "cached": 2, "model": "m1"}
    cur = {"prompt": 20, "completion": 7, "total": 27, "cached": 3, "model": ""}
    out = chat._merge_usage(prev, cur)
    assert out == {"prompt": 30, "completion": 12, "total": 42,
                   "cached": 5, "model": "m1"}
    assert chat._merge_usage(None, cur) is cur
    assert chat._merge_usage(prev, None) is prev


def test_merge_continuation_carries_state():
    """续写收尾：正文拼接、工具/块偏移、用量与 timing 累加、计划写回。"""
    from app.api import chat

    last = {
        "role": "assistant", "content": "原文", "_cont_base": "原文", "_progress": True,
        "tools": [{"tool": "a"}],
        "blocks": [{"kind": "text", "text": "原文"}, {"kind": "tool", "i": 0}],
        "timing": {"total_ms": 100, "rounds": 2},
        "plan": {"steps": [{"text": "旧", "status": "doing"}]},
        "end_reason": {"kind": "stopped"}, "interrupted": True,
        "usage": {"prompt": 10, "completion": 5, "total": 15, "cached": 0, "model": "m1"},
    }
    tr = chat.TurnResult(
        content="新增", blocks=[{"kind": "text", "text": "新增"}],
        tools=[{"tool": "b"}],
        usage={"prompt": 20, "completion": 7, "total": 27, "cached": 1, "model": "m2"},
        total_ms=200, rounds=3, end_reason={"kind": "stopped", "text": "停"},
        interrupted=True, partial=True, done_at=123.0,
        plan={"steps": [{"text": "新", "status": "doing"}]}, tools_base=1)
    out = chat._merge_continuation(last, tr)

    assert out is last
    assert out["content"] == "原文新增"
    assert "_cont_base" not in out and "_progress" not in out
    assert out["interrupted"] is True and out["done_at"] == 123.0
    assert out["tools"] == [{"tool": "a"}, {"tool": "b"}]
    assert [(b["kind"], b.get("i")) for b in out["blocks"]] == [
        ("text", None), ("tool", 0), ("text", None)]
    assert out["blocks"][2].get("ans") is True       # 暂停语义：最后一段正文
    assert out["blocks"][0].get("ans") is None       # 旧正文不再当答案
    assert out["plan"]["steps"][0]["text"] == "新"
    assert out["usage"] == {"prompt": 30, "completion": 12, "total": 42,
                            "cached": 1, "model": "m2"}
    assert out["timing"] == {"total_ms": 300, "rounds": 5}


def test_merge_continuation_without_base_appends():
    """没有 _cont_base（旧数据）时按尾部追加；无 end_reason 时清掉旧结束原因。"""
    from app.api import chat

    last = {"role": "assistant", "content": "旧", "interrupted": True,
            "end_reason": {"kind": "stopped"},
            "timing": {"total_ms": 10, "rounds": 1}, "tools": [{"tool": "a"}]}
    out = chat._merge_continuation(last, chat.TurnResult(content="新", total_ms=5, rounds=1))
    assert out["content"] == "旧新"
    assert "end_reason" not in out
    assert out["interrupted"] is False
    assert out["timing"] == {"total_ms": 15, "rounds": 2}
    assert out["tools"] == [{"tool": "a"}]


def test_merge_continuation_strips_reasoning_when_disabled():
    """关掉落库思考时，新旧 reasoning 块都要剥掉，正文保留。"""
    from app.api import chat

    last = {"role": "assistant", "content": "原文", "_cont_base": "原文",
            "blocks": [{"kind": "text", "text": "原文"},
                       {"kind": "reasoning", "text": "旧思"}]}
    tr = chat.TurnResult(
        content="新", blocks=[{"kind": "reasoning", "text": "新思"},
                              {"kind": "text", "text": "新"}],
        tools_base=0, persist_reason=False)
    out = chat._merge_continuation(last, tr)
    assert [b["kind"] for b in out["blocks"]] == ["text", "text"]


def test_build_new_assistant_message_carries_fields():
    """新消息收尾：来源 / 引用 / 用量 / 工具 / 计划 / 块 / 结束原因都要带上。"""
    from app.api import chat

    s2 = {"messages": []}
    mem = {"changed": [], "ops": []}
    tr = chat.TurnResult(
        content="交付", blocks=[{"kind": "text", "text": "交付"}],
        tools=[{"tool": "read_file"}],
        usage={"prompt": 1, "completion": 1, "total": 2, "cached": 0, "model": "m"},
        total_ms=10, rounds=1, end_reason={"kind": "stopped"},
        interrupted=True, partial=True, done_at=9.0,
        plan={"steps": [{"text": "s", "status": "doing"}]},
        rounds_detail=[{"prompt": 3}], mem_note=mem, ctx_cut=2)
    msg = chat._build_new_assistant_message(s2, tr)

    assert msg["role"] == "assistant"
    assert msg["content"] == "交付" and msg["done_at"] == 9.0
    assert msg["tools"] == [{"tool": "read_file"}]
    assert msg["blocks"][0]["ans"] is True
    assert msg["timing"]["total_ms"] == 10 and msg["timing"]["rounds"] == 1
    assert msg["ctx_cut"] == 2
    assert msg["end_reason"]["kind"] == "stopped"
    assert msg["interrupted"] is True
    assert msg["plan"]["steps"][0]["text"] == "s"
    assert msg["memory"] is mem
    assert msg["usage"]["prompt"] == 1


def test_persist_turn_runs_background_steps(monkeypatch):
    """落库后按顺序触发：持久化 → 会话记忆 → 自动标题；中断时跳过会话记忆。"""
    from app.api import chat
    import app.session_memory as sm

    calls = []
    monkeypatch.setattr(chat, "_persist_session", lambda s: calls.append("persist"))
    monkeypatch.setattr(chat, "_auto_title_session", lambda *a: calls.append("title"))
    monkeypatch.setattr(sm, "maybe_update", lambda *a, **k: calls.append("memory"))

    s2 = {"id": "s1", "messages": [{"role": "assistant", "content": "x"}]}
    chat._persist_turn(s2, "s1", chat.TurnResult(tools=[{"tool": "a"}]),
                       "k", "u", "m")
    assert calls == ["persist", "memory", "title"]
    assert s2["updated_at"] > 0

    calls.clear()
    chat._persist_turn(s2, "s1", chat.TurnResult(interrupted=True), "k", "u", "m")
    assert calls == ["persist", "title"]
