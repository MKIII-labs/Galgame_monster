# -*- coding: utf-8 -*-
"""思考 / 工具的展示顺序落库：块顺序、失败轮、裁剪、续写合并。"""
from app.api.chat import (_append_round_blocks, _empty_final_text,
                          _ensure_old_text_block, _inject_prev_end_note,
                          _is_pseudo_final, _merge_blocks, _needs_plan_gate,
                          _reasoning_cap, _retry_reasoning_block,
                          _strip_reasoning_blocks, _trim_reasoning_blocks)


def test_empty_final_text_distinguishes_stop_from_failure():
    stopped = _empty_final_text(stopped=True)
    assert "已停止" in stopped
    assert "没有给出可执行" not in stopped
    assert "⚠" not in stopped

    assert "无法识别的工具标签" in _empty_final_text(had_unparsed=True)
    assert "没有给出可执行的最终内容" in _empty_final_text()


def test_append_round_blocks_keeps_failed_reasoning():
    blocks = []
    _append_round_blocks(blocks, {"visible": "", "reasoning": "想了一半",
                                  "tool_calls": [], "stream_state": "idle"})
    assert blocks == [{"kind": "reasoning", "text": "想了一半", "retry": True}]

    _append_round_blocks(blocks, {"visible": "答案", "reasoning": "想完了",
                                  "tool_calls": [], "stream_state": "done"})
    assert blocks == [{"kind": "reasoning", "text": "想了一半", "retry": True},
                      {"kind": "reasoning", "text": "想完了"},
                      {"kind": "text", "text": "答案"}]


def test_trim_reasoning_drops_oldest_and_keeps_order():
    blocks = [{"kind": "reasoning", "text": "a" * 6}, {"kind": "text"},
              {"kind": "reasoning", "text": "b" * 6}, {"kind": "tool", "i": 0}]
    out = _trim_reasoning_blocks(blocks, 8)
    texts = [b["text"] for b in out if b["kind"] == "reasoning"]
    assert len(texts) == 1 and texts[0].endswith("b" * 6)
    assert [b["kind"] for b in out] == ["text", "reasoning", "tool"]


def test_trim_reasoning_truncates_a_single_oversized_block():
    out = _trim_reasoning_blocks([{"kind": "reasoning", "text": "x" * 20}], 8)
    assert out[0]["text"].endswith("x" * 8)
    assert out[0]["text"].startswith("…")


def test_reasoning_cap_parsing_and_strip():
    assert _reasoning_cap({}) == 12000
    assert _reasoning_cap({"chat_reasoning_keep_chars": 0}) == 0
    assert _reasoning_cap({"chat_reasoning_keep_chars": -1}) == 0
    assert _reasoning_cap({"chat_reasoning_keep_chars": 123}) == 123
    assert _reasoning_cap({"chat_reasoning_keep_chars": "abc"}) == 12000

    blocks = [{"kind": "reasoning", "text": "x"}, {"kind": "text"}, {"kind": "tool", "i": 0}]
    assert _strip_reasoning_blocks(blocks) == [{"kind": "text"}, {"kind": "tool", "i": 0}]


def test_merge_blocks_offsets_tool_indexes():
    old = [{"kind": "text"}, {"kind": "tool", "i": 0}]
    new = [{"kind": "text"}, {"kind": "tool", "i": 0}]
    out = _merge_blocks(old, new, 3)
    assert out[1] == {"kind": "tool", "i": 0}
    assert out[3] == {"kind": "tool", "i": 3}


def test_retry_reasoning_block_keeps_failed_attempt():
    assert _retry_reasoning_block({"reasoning": "想了一半", "visible": ""}) == {
        "kind": "reasoning", "text": "想了一半", "retry": True}
    assert _retry_reasoning_block({"reasoning": ""}) is None
    assert _retry_reasoning_block({}) is None

    # 裁剪只改文本，不能把「上次尝试」标记弄丢
    out = _trim_reasoning_blocks(
        [{"kind": "reasoning", "text": "x" * 20, "retry": True}], 8)
    assert out[0]["retry"] is True


def test_api_chat_never_shadows_handler_variable():
    """_api_chat(h) 里的 h 是 HTTP 连接对象。

    函数体内任何 `h = ...` 都会把连接覆盖成字符串，之后所有 SSE 写入抛
    'str' object has no attribute 'wfile'，前端误判断线（NOTE 43.168）。
    """
    import inspect
    import re
    from app.api import chat
    src = inspect.getsource(chat._api_chat)
    assert not re.search(r"(?m)^\s+h\s*=", src)


def test_ensure_old_text_block_for_v2_continuation():
    # 没有 blocks 的旧消息：补一条正文块
    assert _ensure_old_text_block({"content": "旧答案"}) == [
        {"kind": "text", "text": "旧答案"}]
    # V2 只有标记、没有正文：把旧正文补在最前面
    v2 = {"content": "旧答案", "blocks": [{"kind": "text"}, {"kind": "tool", "i": 0}]}
    assert _ensure_old_text_block(v2)[0] == {"kind": "text", "text": "旧答案"}
    # 已经是 V3（带正文）就不重复补
    v3 = {"content": "旧答案", "blocks": [{"kind": "text", "text": "旧答案"}]}
    assert _ensure_old_text_block(v3) == [{"kind": "text", "text": "旧答案"}]
    # 没有正文的旧消息保持原样
    assert _ensure_old_text_block({"content": ""}) == []


def test_attach_dyn_tail_keeps_system_stable():
    """A档：动态上下文与时钟贴到最后一条 user 消息，system 保持逐字不变。"""
    from app.api.chat import _attach_dyn_tail
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "这一轮"}]
    tail = _attach_dyn_tail(msgs, "【记忆】x", "【当前时间】y")
    assert msgs[0]["content"] == "SYS"          # 系统提示一个字节都不变（前缀缓存）
    assert msgs[-1]["content"].endswith(tail)
    assert "【记忆】x" in msgs[-1]["content"] and "【当前时间】y" in msgs[-1]["content"]
    assert msgs[1]["content"] == "第一轮"        # 只贴本轮最后一条 user
    assert _attach_dyn_tail(msgs, "", "") == ""  # 没有动态块时不动消息


# ============================================================
# 执行前的计划闸门
# ============================================================
def test_plan_gate_skips_single_read():
    """只读一个文件 / 搜一次不算任务，不该拦。"""
    assert _needs_plan_gate([{"tool": "read_file"}], agent_on=True) is False
    assert _needs_plan_gate([{"tool": "search_code"}], agent_on=True) is False
    assert _needs_plan_gate([], agent_on=True) is False


def test_plan_gate_requires_plan_for_writes_and_commands():
    """写入 / 命令 / Git 提交哪怕是单次，也必须先出计划。"""
    assert _needs_plan_gate([{"tool": "write_file"}], agent_on=True) is True
    assert _needs_plan_gate([{"tool": "edit_file"}], agent_on=True) is True
    assert _needs_plan_gate([{"tool": "run_command"}], agent_on=True) is True
    assert _needs_plan_gate([{"tool": "git_commit"}], agent_on=True) is True


def test_plan_gate_requires_plan_for_multi_step_reads():
    """同一批里读多处 = 多步任务，也要先列计划。"""
    calls = [{"tool": "read_file"}, {"tool": "search_code"}]
    assert _needs_plan_gate(calls, agent_on=True) is True
    # 没开文件 Agent 时，能力类工具（日程 / 提醒）不拦
    assert _needs_plan_gate([{"tool": "calendar_create"}, {"tool": "set_reminder"}],
                            agent_on=False) is False


def test_plan_gate_allows_batch_that_already_has_plan():
    """同一批里带了 update_plan 就放行，避免多做一轮。"""
    calls = [{"tool": "update_plan"}, {"tool": "write_file"}]
    assert _needs_plan_gate(calls, agent_on=True) is False


# ============================================================
# 占位文本（「（调用工具）」）与上一轮结束原因
# ============================================================
def test_pseudo_final_detection():
    assert _is_pseudo_final("（调用工具）") is True
    assert _is_pseudo_final("(调用工具)") is True
    assert _is_pseudo_final("调用工具") is True
    assert _is_pseudo_final("（调用工具）（调用工具）") is True
    assert _is_pseudo_final("（调用工具）。") is True
    assert _is_pseudo_final("我调用了工具，结果是……") is False
    assert _is_pseudo_final("调用工具失败，因为脚本不存在") is False
    assert _is_pseudo_final("") is False
    assert _is_pseudo_final("（调用工具）" * 30) is False   # 太长不认


def test_inject_prev_end_note():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "继续"}]
    _inject_prev_end_note(msgs, {"text": "连续多轮没有结论，已停止"})
    assert len(msgs) == 3
    assert msgs[1]["role"] == "user" and "上一轮以" in msgs[1]["content"]
    assert msgs[2]["content"] == "继续"        # 真正的提问仍在最后

    msgs2 = [{"role": "user", "content": "x"}]
    _inject_prev_end_note(msgs2, None)
    assert len(msgs2) == 1                      # 没有结束原因就不动
