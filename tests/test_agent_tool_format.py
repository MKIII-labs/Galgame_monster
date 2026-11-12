# -*- coding: utf-8 -*-
"""工具调用格式兼容：模型有时不用 ```tool 围栏，而是吐 DeepSeek 原生标记（DSML）。

本会话真实踩坑：不认这个格式 → 整块标记直接显示给用户、工具不执行、本轮静默结束，
表现成「输出中断」。所以这里把「识别 + 隐藏 + 执行」三件事都锁住。
"""
from app import agent
from app.mem_tool import DSML_CLOSE, DSML_OPEN


def test_parse_calls_fence_envelope():
    calls = agent.parse_calls('{"calls":[{"tool":"list_dir","args":{"path":"."}}]}')
    assert calls == [{"tool": "list_dir", "args": {"path": "."}}]


def test_adopt_bare_calls_from_visible():
    """裸 {"calls":...} 必须被提升为工具调用，而不是当正文显示"""
    from app.api import chat
    rr = {"visible": '{"calls":[{"tool":"bili.up","args":{"name":"可可味","pages":2}}]}',
          "tool_calls": [], "tool_block": ""}
    assert chat._adopt_bare_calls(rr) is True
    assert rr["visible"] == ""
    assert rr["tool_calls"][0]["tool"] == "bili.up"
    assert rr["tool_block"].startswith('{"calls"')


def test_adopt_bare_calls_keeps_normal_text():
    from app.api import chat
    rr = {"visible": "普通回答，没有工具", "tool_calls": [], "tool_block": ""}
    assert chat._adopt_bare_calls(rr) is False
    assert rr["visible"] == "普通回答，没有工具"


def test_strip_final_bare_calls():
    from app.api import chat
    text = ('用 UID 直接拉投稿列表。\n\n'
            '{"calls":[{"tool":"bili.up","args":{"name":"198439","pages":2}}]}')
    cleaned, found = chat._strip_final_bare_calls(text)
    assert found is True
    assert cleaned == "用 UID 直接拉投稿列表。"
    normal, found2 = chat._strip_final_bare_calls("普通回答")
    assert found2 is False and normal == "普通回答"


def test_pending_tool_claim_detection():
    from app.api import chat
    assert chat._claims_pending_tool("这次直接拉投稿列表。工具已发出，等结果回来。")
    assert chat._claims_pending_tool("我现在查一下")
    assert chat._claims_pending_tool("我先看一下文件")
    assert chat._claims_pending_tool("我来建个日程")
    assert chat._claims_pending_tool("我先读一下这个文件")
    assert not chat._claims_pending_tool("普通回答")
    assert chat._needs_tool_claim_nudge("工具已发出", False, False) is True
    assert chat._needs_tool_claim_nudge("工具已发出", True, False) is False
    assert chat._needs_tool_claim_nudge("工具已发出", False, True) is False
    # 非 Agent 会话只剩查询能力：只认搜索类声明，讲课口吻不算
    assert chat._claims_pending_tool("我现在查一下", agent_mode=False)
    assert chat._claims_pending_tool("我先搜索一下这个结论", agent_mode=False)
    assert not chat._claims_pending_tool("我先把这一行加上去", agent_mode=False)
    assert not chat._claims_pending_tool("我先看一下文件", agent_mode=False)


def test_parse_calls_ndjson():
    # DeepSeek 原生标记里就是「一行一个 JSON 对象」
    body = ('{"tool":"list_dir","args":{"path":"."}}\n'
            '{"tool":"read_file","args":{"path":"readme.md"}}\n')
    calls = agent.parse_calls(body)
    assert [c["tool"] for c in calls] == ["list_dir", "read_file"]
    assert calls[1]["args"] == {"path": "readme.md"}


def test_parse_calls_keeps_marker_head_out_of_the_way():
    body = DSML_OPEN + "\n" + '{"tool":"list_dir","args":{"path":"app"}}' + "\n" + DSML_CLOSE
    calls = agent.parse_calls(body)
    assert calls and calls[0]["tool"] == "list_dir"
    assert calls[0]["args"] == {"path": "app"}


def test_extractor_hides_native_block():
    mx = agent.ToolExtractor()
    vis = mx.feed("看完了。" + DSML_OPEN + "\n")
    vis += mx.feed('{"tool":"list_dir","args":{"path":"."}}\n')
    vis += mx.feed(DSML_CLOSE)
    mx.finish()
    assert vis.strip() == "看完了。"              # 标记与块内容都不该显示给用户
    assert "list_dir" in mx.block_text
    assert agent.parse_calls(mx.block_text)[0]["tool"] == "list_dir"


def test_extractor_still_handles_our_fence():
    mx = agent.ToolExtractor()
    vis = mx.feed("正文\n```tool\n")
    vis += mx.feed('{"calls":[{"tool":"read_file","args":{"path":"a.txt"}}]}\n')
    vis += mx.feed("```")
    mx.finish()
    assert vis.strip() == "正文"
    assert agent.parse_calls(mx.block_text)[0]["tool"] == "read_file"


def test_extractor_ignores_inline_fence_literal():
    """正文里提到的 ```tool（不在行首）不是控制块起点 —— 不能吞掉后半段回答。

    真实踩坑：模型讲解协议时写了行内的 ```tool，旧实现按「任意位置命中」把它当控制块
    起点，后半段回答被整块吞掉。
    """
    mx = agent.ToolExtractor()
    text = "说明：工具请求写在 ```tool 代码块里，块内是 JSON。以上。"
    vis = "".join(mx.feed(text[i:i + 5]) for i in range(0, len(text), 5))   # 小 chunk 模拟流式
    mx.finish()
    assert vis.strip() == text.strip()
    assert mx.block_text == ""


def test_extractor_ignores_inline_memory_literal():
    from app.mem_tool import MemoryExtractor
    mx = MemoryExtractor()
    text = "记忆写入用 ```memory 块表达。就这样。"
    vis = "".join(mx.feed(text[i:i + 4]) for i in range(0, len(text), 4))
    mx.finish()
    assert mx.block_text == ""
    assert vis.strip() == text.strip()


# ============================================================
# 工具循环的收尾判定：**不能静默收尾**
# 本会话真实踩坑：模型刚说「我先真读，读完再答」并请求了工具，
# 一旦轮次用尽（或工具块解析不出来）就被 break 掉 → 用户看到「输出被打断」。
# ============================================================

def _act(**kw):
    from app.api.chat import _tool_loop_action
    base = dict(has_calls=False, has_bad_block=False, stopped=False,
                rounds=0, max_rounds=8, nudged_limit=False, nudged_format=False)
    base.update(kw)
    return _tool_loop_action(**base)


def test_tool_loop_normal_stop():
    assert _act() == "stop"                       # 没有工具请求 → 正常收尾


def test_tool_loop_runs_tools():
    assert _act(has_calls=True, rounds=3) == "run"


def test_tool_loop_nudges_when_rounds_spent():
    assert _act(has_calls=True, rounds=8) == "nudge_limit"


def test_tool_loop_zero_means_unlimited():
    """max_rounds = 0 是「不限」：轮数再大也不提醒收尾"""
    assert _act(has_calls=True, rounds=200, max_rounds=0) == "run"


def test_tool_loop_reports_incomplete_instead_of_silent_stop():
    """轮次用尽且已提醒过，模型还要工具 → 必须走 stop_incomplete。

    以前这里返回 "stop" → 循环静默 break，用户只看到回答停在半句话、
    完全不知道发生了什么（实测：46 个文件的任务撞上 8 轮 × 6 调用 = 48 次后断尾）。
    """
    assert _act(has_calls=True, rounds=8, nudged_limit=True) == "stop_incomplete"


def test_tool_loop_nudges_on_unparsable_block():
    assert _act(has_bad_block=True) == "nudge_format"
    assert _act(has_bad_block=True, nudged_format=True) == "stop"


def test_tool_loop_user_stop_wins():
    assert _act(has_calls=True, stopped=True) == "stop"
    assert _act(has_bad_block=True, stopped=True) == "stop"


def test_tool_loop_finish_is_explicit_stop():
    """显式 finish（Trae 的 task_done）：调过就直接收尾，不再等「没有工具调用」。"""
    assert _act(finished=True) == "stop"
    assert _act(finished=True, has_calls=True) == "stop"


# ============================================================
# 伪工具标签 / 空话核对
# 真实踩坑：模型写 <tool调用> 而不是 ```tool，系统没执行，
# 后面紧跟的 `</tool>## 标题` 还会被当成普通文本，标题显示成源码。
# ============================================================

def test_clean_assistant_text_strips_pseudo_tool_tags():
    from app.api.chat import _clean_assistant_text
    raw = "先看格式。\n\n<tool调用>\n\n</tool>## 每日 note 方案\n\n内容"
    out = _clean_assistant_text(raw)
    assert "<tool" not in out
    assert "\n## 每日 note 方案" in out


def test_claims_done_detects_completion_phrases():
    from app.api.chat import _claims_done
    assert _claims_done("已加入工作台 → 日程")
    assert _claims_done("已创建日程")
    assert _claims_done("已经写入会话记忆")
    assert _claims_done("复习提醒已入会话记忆")
    assert _claims_done("已改短")
    assert _claims_done("已经改好了")
    assert not _claims_done("建议加入日程")
    assert not _claims_done("明天再创建")


def test_claims_memory_detects_false_memory_edits():
    """真实踩坑：模型说「已改短 / 现在真的改」，但没有输出 ```memory 块。"""
    from app.api.chat import _claims_memory
    assert _claims_memory("已改短。")
    assert _claims_memory("记忆已更新")
    assert _claims_memory("现在真的改。")
    assert not _claims_memory("没有改动")
    assert not _claims_memory("上一轮没有改")
    assert not _claims_memory("已更新日程")      # 日程不是记忆，别误触发补做


def test_needs_memory_nudge():
    from app.api.chat import _needs_memory_nudge
    assert _needs_memory_nudge("已改短", [], False)
    assert not _needs_memory_nudge("已改短", [{"op": "update"}], False)
    assert not _needs_memory_nudge("已改短", [], True)      # 只补一次
    assert not _needs_memory_nudge("没有改动", [], False)


def test_needs_memory_format_nudge():
    from app.api.chat import _needs_memory_format_nudge
    assert _needs_memory_format_nudge('{"ops":[bad]}', True, [], False)
    assert not _needs_memory_format_nudge("", True, [], False)
    assert not _needs_memory_format_nudge("{...}", False, [], False)   # 未闭合会还回正文
    assert not _needs_memory_format_nudge("{...}", True, [{"op": "add"}], False)


def test_needs_empty_final_nudge():
    from app.api.chat import _needs_empty_final_nudge
    assert _needs_empty_final_nudge("", "想了很多", False, False, False)
    assert not _needs_empty_final_nudge("有正文", "想了很多", False, False, False)
    assert not _needs_empty_final_nudge("", "", False, False, False)
    assert not _needs_empty_final_nudge("", "想了很多", True, False, False)
    assert not _needs_empty_final_nudge("", "想了很多", False, True, False)
    assert not _needs_empty_final_nudge("", "想了很多", False, False, True)


def test_memory_extractor_exposes_closed_invalid_block():
    """IDX 188 的根因：块已闭合但 JSON 非法 → finish() 返回空，旧逻辑整块静默丢弃。"""
    from app.mem_tool import MemoryExtractor
    mx = MemoryExtractor()
    mx.feed("```memory\n{\"ops\":[bad]}\n```")
    assert mx.finish() == ""          # 已闭合：不还回正文
    assert mx.closed is True
    assert mx.block_text              # 内容仍在，供上层重试 / 诊断
    assert mx.ops() == []


# ============================================================
# 裸工具 JSON：模型不写 ```tool 围栏，直接在末尾吐 {"calls":[...]}
# 真实踩坑：整段 JSON 被当正文渲染，用户看到源码；工具也没执行。
# ============================================================

def test_extract_bare_calls_from_tail():
    from app.llm import _extract_bare_calls
    text = '先说结论。\n\n{"calls":[{"tool":"calendar.create","args":{"title":"x"}}]}'
    body, block = _extract_bare_calls(text)
    assert body == '先说结论。'
    assert agent.parse_calls(block)[0]["tool"] == "calendar.create"


def test_extract_bare_calls_keeps_plain_json():
    from app.llm import _extract_bare_calls
    text = '配置：{"a":1}'
    body, block = _extract_bare_calls(text)
    assert body == text and block == ""


def test_extract_bare_calls_handles_json_fence():
    from app.llm import _extract_bare_calls
    text = '正文\n\n```json\n{"calls":[{"tool":"list_dir","args":{}}]}\n```'
    body, block = _extract_bare_calls(text)
    assert body == '正文'
    assert agent.parse_calls(block)[0]["tool"] == "list_dir"


# ============================================================
# <tool calls> / <tool_calls>：DeepSeek 原生标记被文本化后的形态
# 真实踩坑：它出现在正文**中间**、闭合标签时有时无，
# 「末尾裸 JSON」和「伪标签」两条兜底都碰不到 → 源码落库并污染下一轮历史。
# ============================================================

def test_extract_tagged_calls_in_middle():
    from app.llm import _extract_bare_calls
    text = ('先看格式。\n\n<tool calls>\n'
            '{"calls":[{"tool":"list_dir","args":{"path":"."}}]}\n'
            '</tool calls>\n\n等待返回。')
    body, block = _extract_bare_calls(text)
    assert body == '先看格式。\n\n\n\n等待返回。'
    assert 'calls' not in body and 'tool calls' not in body
    assert agent.parse_calls(block)[0]["tool"] == "list_dir"


def test_extract_tagged_calls_without_closer():
    """漏写闭合标签：后面还有正文，不能把正文吞进调用块"""
    from app.llm import _extract_bare_calls
    text = ('A<tool calls>{"calls":[{"tool":"read_file","args":{"path":"a.md"}}]}'
            '\n\n中间正文\n')
    body, block = _extract_bare_calls(text)
    assert 'A' in body and '中间正文' in body and 'read_file' not in body
    assert agent.parse_calls(block)[0]["tool"] == "read_file"


def test_extract_tagged_calls_underscore_and_merge():
    from app.llm import _extract_bare_calls
    text = ('<tool_calls>{"calls":[{"tool":"list_dir","args":{}}]}</tool_calls>'
            '正文'
            '<tool calls>{"calls":[{"tool":"read_file","args":{"path":"b"}}]}</tool calls>')
    body, block = _extract_bare_calls(text)
    assert body == '正文'
    assert [c["tool"] for c in agent.parse_calls(block)] == ["list_dir", "read_file"]


def test_extract_tagged_calls_keeps_plain_text():
    """标签后面不是调用 JSON（比如用户让模型讲协议）：不执行，正文保留、标签清掉"""
    from app.llm import _extract_bare_calls
    text = "讲解一下 <tool calls> 这个标签是什么。"
    body, block = _extract_bare_calls(text)
    assert block == ""
    assert "讲解一下" in body and "这个标签是什么" in body
    assert "<tool" not in body


def test_strip_ctrl_markers_cleans_tool_tags():
    """落库前必须把标签清掉，否则下一轮模型照着历史继续吐"""
    from app.llm import _strip_ctrl_markers
    out = _strip_ctrl_markers("<tool calls>\n正文\n</tool_calls>")
    assert 'tool' not in out and '正文' in out


# ============================================================
# 第四 ~ 六种形态：<parameter name="calls"> / 自闭合 / 引号残留 /
# 正文中间的 ```json 围栏 / 行首裸 JSON
# 真实数据里模型一直在换模板，枚举标签是追不上的 —— 统一规则：
# 标签与围栏只是信号，块边界由能解析成调用的 JSON 决定。
# ============================================================

def test_extract_parameter_tag_with_array():
    from app.llm import _extract_bare_calls
    text = ('先说明。\n\n<parameter name="calls">'
            '[{"tool":"read_file","args":{"path":"a.md"}},'
            '{"tool":"run_command","args":{"argv":["node","x.js"]}}]'
            '</parameter>\n\n继续。')
    body, block = _extract_bare_calls(text)
    assert 'parameter' not in body and 'read_file' not in body
    assert '先说明。' in body and '继续。' in body
    assert [c["tool"] for c in agent.parse_calls(block)] == ["read_file", "run_command"]


def test_self_closing_tag_is_only_noise():
    """<tool calls/> 只有意图没有参数：不执行，只让 _strip_ctrl_markers 清掉"""
    from app.llm import _extract_bare_calls, _strip_ctrl_markers
    text = "已经写入。\n\n<tool calls/>\n\n现在读回确认。"
    body, block = _extract_bare_calls(text)
    assert block == ""
    assert "已经写入" in body and "现在读回确认" in body
    assert "<tool" not in _strip_ctrl_markers(body)


def test_extract_tag_with_stray_quote():
    from app.llm import _extract_bare_calls
    text = ('<tool calls">\n{"calls":[{"tool":"delete_file","args":{"path":"x"}}]}\n'
            '</tool calls>\n结束。')
    body, block = _extract_bare_calls(text)
    assert 'delete_file' not in body and '<tool' not in body
    assert '结束。' in body
    assert agent.parse_calls(block)[0]["tool"] == "delete_file"


def test_extract_json_fence_in_middle():
    from app.llm import _extract_bare_calls
    text = '前文\n\n```json\n{"calls":[{"tool":"list_dir","args":{}}]}\n```\n\n后文'
    body, block = _extract_bare_calls(text)
    assert body == '前文\n\n\n\n后文'
    assert agent.parse_calls(block)[0]["tool"] == "list_dir"


def test_extract_bare_block_at_line_start():
    from app.llm import _extract_bare_calls
    text = '直接写脚本：\n\n{"tool":"write_file","args":{"path":"a.js","content":"1"}}\n\n然后运行。'
    body, block = _extract_bare_calls(text)
    assert body == '直接写脚本：\n\n\n\n然后运行。'
    assert agent.parse_calls(block)[0]["tool"] == "write_file"


def test_bare_block_with_unknown_tool_kept():
    """示例 JSON 用的是虚构工具名 → 不当调用，正文原样保留"""
    from app.llm import _extract_bare_calls
    text = '示例：\n\n{"tool":"not_a_real_tool","args":{}}\n\n完。'
    body, block = _extract_bare_calls(text)
    assert block == "" and 'not_a_real_tool' in body


def test_broken_labelled_json_is_dropped_not_executed():
    """标签后面是坏 JSON（字符串里有裸换行）：边界确定 → 整段隐去，但不执行"""
    from app.llm import _extract_bare_calls
    text = ('<tool calls>\n{"calls":[{"tool":"write_file","args":{"content":"a\nb"}}]}'
            '\n</tool calls>\n正文。')
    body, block = _extract_bare_calls(text)
    assert block == ""
    assert "正文。" in body
    assert "write_file" not in body and "<tool" not in body


# ============================================================
# 第 8 代伪装：占位符标签 + 模型自演工具结果（白名单清洗）
# ============================================================

def test_placeholder_tag_without_json_is_noise():
    """`<tool calls here>` 这种占位符：没有 JSON，只丢标签"""
    from app.llm import _extract_bare_calls
    body, block = _extract_bare_calls("先确认行号。\n\n<tool calls here>\n\n再读原文。")
    assert block == ""
    assert "先确认行号。" in body and "再读原文。" in body
    assert "<tool" not in body


def test_fake_tool_result_dropped_with_content():
    """模型自演的工具结果：标签和内容一起删（内容不是 JSON 也要删）"""
    from app.llm import _extract_bare_calls
    text = ("<tool_result>thinking\nI'm checking the game file's structure.\n</tool_result>\n"
            "工具位置已确认。")
    body, block = _extract_bare_calls(text)
    assert block == ""
    assert "工具位置已确认。" in body
    assert "tool_result" not in body and "checking the game file" not in body


def test_normal_tags_and_svg_are_kept():
    """正常 HTML / SVG / 占位符都不能被当成协议噪音（真实会话里踩过）"""
    from app.llm import _strip_unknown_tags
    text = ("这是一段 <b>加粗</b> 与 <span class=\"x\">行内</span>，<br/> 换行。\n"
            '<svg><linearGradient id="g"><stop offset="0%"/></linearGradient>'
            '<ellipse cx="1" cy="2"/></svg>\n'
            "文件名格式：<YYYYMMDD>_<seed>.png")
    assert _strip_unknown_tags(text) == text


def test_long_paired_unknown_tag_keeps_content():
    """超长配对块只删标签：防止模型把整篇答案包进标签时被误删"""
    from app.llm import _strip_unknown_tags
    body = "正文" * 1200                    # 2400 字 > _PROTO_PAIR_MAX
    out = _strip_unknown_tags("<tool_result>\n" + body + "\n</tool_result>")
    assert body in out and "tool_result" not in out


def test_url_like_angle_brackets_untouched():
    """<https://...> 这类自动链接不能被当成标签删掉"""
    from app.llm import _strip_unknown_tags
    text = "参考 <https://example.com/a?b=1> 与 a < b > c。"
    assert _strip_unknown_tags(text) == text


def test_parameter_tag_with_array_still_executes():
    """白名单清洗不能把 <parameter name="calls"> 里的调用一起删掉"""
    from app import agent
    from app.llm import _extract_bare_calls
    text = ('<parameter name="calls">\n[{"tool":"read_file","args":{"path":"a.py"}}]\n'
            '</parameter>')
    body, block = _extract_bare_calls(text)
    assert [c["tool"] for c in agent.parse_calls(block)] == ["read_file"]
    assert "<parameter" not in body


# ============================================================
# 收尾 / 恢复判定：对齐 Claude Code 的转移语义（纯函数，便于断言）
# ============================================================

def test_truncation_action_splits_window_and_output_cap():
    """窗口满和输出额度用完必须分开：前者先压缩，后者直接续写。"""
    from app.api.chat import _truncation_action
    base = dict(finish_reason="length", has_calls=False, stopped=False,
                gal=False, nudged=False, window_limited=False,
                window_compressed=False)
    assert _truncation_action(**base) == "nudge"
    assert _truncation_action(**dict(base, window_limited=True)) == "compress_nudge"
    assert _truncation_action(**dict(base, window_limited=True,
                                     window_compressed=True)) == "nudge"
    assert _truncation_action(**dict(base, finish_reason="stop")) == "none"
    assert _truncation_action(**dict(base, has_calls=True)) == "none"
    assert _truncation_action(**dict(base, nudged=True)) == "none"
    assert _truncation_action(**dict(base, gal=True)) == "none"


def test_break_continue_action_requires_visible_and_no_calls():
    """断点续写的前提：有正文、没有待执行工具（否则会重复副作用）。"""
    from app.api.chat import _break_continue_action
    base = dict(stream_state="network", visible="半句", has_calls=False,
                stopped=False, used=0, limit=2)
    assert _break_continue_action(**base) is True
    assert _break_continue_action(**dict(base, used=2)) is False
    assert _break_continue_action(**dict(base, limit=0)) is False
    assert _break_continue_action(**dict(base, has_calls=True)) is False
    assert _break_continue_action(**dict(base, visible="  ")) is False
    assert _break_continue_action(**dict(base, stream_state="billing")) is False


def test_pseudo_final_action_retries_once_then_stops():
    """占位正文：先提醒一次，再犯就收尾，不能进自动续跑。"""
    from app.api.chat import _pseudo_final_action
    base = dict(is_pseudo=True, stopped=False, gal=False, nudged=False)
    assert _pseudo_final_action(**base) == "retry"
    assert _pseudo_final_action(**dict(base, nudged=True)) == "stop"
    assert _pseudo_final_action(**dict(base, is_pseudo=False)) == "none"
    assert _pseudo_final_action(**dict(base, gal=True)) == "none"
    assert _pseudo_final_action(**dict(base, stopped=True)) == "none"


def test_stop_hook_action_blocks_at_most_limit_times():
    """Stop hook：能拦收尾，但每回合只能拦 MAX_STOP_HOOK_BLOCKS 次。"""
    from app.api.chat import _stop_hook_action, MAX_STOP_HOOK_BLOCKS
    base = dict(decision="block", used=0, limit=MAX_STOP_HOOK_BLOCKS,
                stopped=False, gal=False)
    assert _stop_hook_action(**base) == "block"
    assert _stop_hook_action(**dict(base, used=MAX_STOP_HOOK_BLOCKS)) == "none"
    assert _stop_hook_action(**dict(base, decision="")) == "none"
    assert _stop_hook_action(**dict(base, stopped=True)) == "none"
    assert _stop_hook_action(**dict(base, gal=True)) == "none"


def test_stream_prefetch_runs_reads_but_not_after_write(monkeypatch):
    """流式预取：只读工具提前跑；出现写工具后，后续的读必须等写完成。"""
    import threading

    from app.api import chat

    calls = []

    def fake_run_tool(sandbox, name, args, **kw):
        calls.append(name)
        return ("ok", None, False)

    monkeypatch.setattr(chat.agent, "run_tool", fake_run_tool)
    ev = threading.Event()
    ready, drain = chat._stream_prefetch_sink(
        sandbox=".", allow_outside=False, caps={}, stop_event=ev, enabled=True)

    ready({"tool": "read_file", "args": {"path": "a.txt"}})
    ready({"tool": "write_file", "args": {"path": "a.txt", "content": "x"}})
    ready({"tool": "read_file", "args": {"path": "b.txt"}})
    out = drain()

    assert calls == ["read_file"]           # 写本身不跑；写之后的读也不再提前跑
    assert len(out) == 1
