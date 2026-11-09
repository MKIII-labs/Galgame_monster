# -*- coding: utf-8 -*-
"""上下文：估算口径、压缩边界、摘要拆分、兜底裁剪与恢复"""
import pytest

from app import context, llm
from app.sessions import invalidate_compression


@pytest.fixture(autouse=True)
def _small_window(monkeypatch):
    """固定窗口，避免测试依赖本机真实配置"""
    monkeypatch.setattr(context, "load_config",
                        lambda: {"chat_context_limit": 1000, "chat_reserve_output": 100})


def _msg(role, text, **kw):
    m = {"role": role, "content": text}
    m.update(kw)
    return m


def test_estimate_tokens_basic():
    assert context.estimate_tokens("") == 0
    assert context.estimate_tokens("你好") == 1        # 2 字 × 0.6
    assert context.estimate_tokens("hello") == 1       # 5 字符 × 0.28
    assert context.estimate_tokens("hello world") == 3  # 10 字母 + 1 空白


def test_tools_tokens_scales_with_schema():
    assert context.tools_tokens([]) == 0
    small = context.tools_tokens([{"name": "read_file", "description": "读文件"}])
    big = context.tools_tokens([{"name": "x", "description": "很长的描述" * 100}])
    assert small > 0
    assert big > small


def test_recover_overflow_folds_then_drops():
    msgs = [_msg("system", "sys")]
    for _ in range(40):
        msgs.append(_msg("user", context.TOOL_MSG_PREFIX + "\n" + ("x" * 2000)))
    msgs.append(_msg("assistant", "latest"))
    out, folded, dropped = context.recover_overflow(msgs, 1200)
    assert folded > 0
    assert dropped > 0
    assert context.estimate_messages_tokens(out) <= 1260  # 允许提示语的结构开销
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "latest"


def test_last_input_detail_prefers_message_timing():
    s = {"messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "usage": {"prompt": 100, "last_prompt": 60},
         "timing": {"rounds": 2, "total_ms": 1234,
                    "rounds_detail": [{"n": 1, "prompt": 40},
                                      {"n": 2, "prompt": 60}]}},
    ]}
    d = context.last_input_detail(s)
    assert d["source"] == "message"
    assert d["rounds"] == 2 and len(d["rounds_detail"]) == 2
    assert d["usage"]["prompt"] == 100
    assert context.last_input_detail({"messages": []}) is None


def test_last_input_detail_falls_back_to_usage_log(monkeypatch):
    monkeypatch.setattr("app.usage.rounds_for_turn",
                        lambda done, rounds=0, session_id="": [{"n": 1, "prompt": 7}])
    s = {"messages": [{"role": "assistant", "usage": {"prompt": 7},
                       "timing": {"rounds": 1}, "done_at": 123}]}
    d = context.last_input_detail(s)
    assert d["source"] == "usage_log"
    assert d["rounds_detail"][0]["prompt"] == 7


def test_last_input_detail_skips_turns_without_detail():
    s = {"messages": [
        {"role": "assistant", "usage": {"prompt": 1},
         "timing": {"rounds": 1, "rounds_detail": [{"n": 1, "prompt": 1}]}},
        {"role": "assistant", "usage": {"prompt": 2}, "timing": {"rounds": 1}},
    ]}
    d = context.last_input_detail(s)
    assert d["usage"]["prompt"] == 1


def test_keep_from_uses_calib():
    msgs = [_msg("user", "x" * 1000) for _ in range(20)]
    a = context.keep_from_by_tokens(msgs, 0, 10000, 0.25, 1.0)
    b = context.keep_from_by_tokens(msgs, 0, 10000, 0.25, 2.0)
    assert b >= a


def test_context_counts_attachment_and_vision():
    plain = {"messages": [_msg("user", "看这个")]}
    with_doc = {"messages": [_msg("user", "看这个", doc_text="正" * 1000,
                                  doc={"name": "a.pdf"})]}
    with_img = {"messages": [_msg("user", "这是什么", vision="图片内容" * 100)]}
    a = context.session_context(plain, "")
    b = context.session_context(with_doc, "")
    c = context.session_context(with_img, "")
    assert b["used"] > a["used"]
    assert b["parts"]["attachment"] > 0
    assert c["used"] > a["used"]


def test_extra_tokens_counted_as_image():
    s = {"messages": [_msg("user", "hi")]}
    ctx = context.session_context(s, "", extra_tokens=1600)
    assert ctx["parts"]["image"] == 1600
    assert ctx["used"] >= 1600


def test_keep_from_respects_token_budget():
    msgs = [_msg("user", "字" * 100) for _ in range(20)]   # 每条约 60 tokens
    keep = context.keep_from_by_tokens(msgs, 0, 1000)      # 预算 250 → 大约保留 4 条
    assert keep == 12                                      # 但至少保留 8 条
    short = [_msg("user", "短") for _ in range(50)]        # 全部能装下
    assert context.keep_from_by_tokens(short, 0, 1000) == 0


def test_context_limits_only_reads_explicit_setting(monkeypatch):
    """窗口只认设置里的值：0 = 不限（不做任何模型窗口猜测）。"""
    monkeypatch.setattr(context, "load_config",
                        lambda: {"chat_context_limit": 0, "chat_reserve_output": 4096})
    monkeypatch.setattr(context, "get_api_conf", lambda p: ("k", "http://x", "deepseek-flash"))
    assert context.context_limits() == (0, 0)
    assert context.context_limits("deepseek-flash") == (0, 0)
    monkeypatch.setattr(context, "load_config",
                        lambda: {"chat_context_limit": 900000, "chat_reserve_output": 4096})
    assert context.context_limits("deepseek-flash")[0] == 900000


def test_context_limits_reserve_default_and_zero(monkeypatch):
    """预留默认 16384；显式填 0 表示不预留，不能被 `or 默认值` 吃掉。"""
    monkeypatch.setattr(context, "model_context_limit",
                        lambda model, cfg=None: 128000)
    monkeypatch.setattr(context, "load_config", lambda: {"chat_context_limit": 0})
    assert context.context_limits("deepseek-flash")[1] == 16384
    monkeypatch.setattr(context, "load_config",
                        lambda: {"chat_context_limit": 0, "chat_reserve_output": 0})
    assert context.context_limits("deepseek-flash")[1] == 0


def test_split_summary():
    text = "【摘要】\n目标 A\n【记忆要点】\n- 结论一\n- （无）\n- 结论二"
    summary, points = context._split_summary(text)
    assert "目标 A" in summary and "【记忆要点】" not in summary
    assert points == ["结论一", "结论二"]


def test_compress_history_uses_light_model(monkeypatch):
    monkeypatch.setattr(context, "get_api_conf",
                        lambda p: ("k", "http://x", "main-model"))
    # 不依赖真实 config.json：给一个小窗口，才可能触发压缩
    monkeypatch.setattr(context, "context_limits", lambda model="": (4000, 500))
    calls = {}

    def fake(api_key, api_url, api_model, messages, max_tokens=96, source="gal",
             stop_event=None):
        calls.update({"model": api_model, "source": source, "max_tokens": max_tokens})
        return "【摘要】\n早先聊了 A\n【记忆要点】\n- 要点一"

    monkeypatch.setattr(llm, "_quick_chat", fake)
    s = {"messages": [_msg("user", "字" * 100) for _ in range(20)]}
    assert context.compress_history(s) is True
    assert s["summary"].startswith("早先聊了")
    assert s["summary_upto"] > 0
    assert s["summary_mem"] == ["要点一"]
    assert s["summary_at"] > 0
    mark = s["compress_marks"][-1]
    assert mark["index"] == len(s["messages"])
    assert mark["upto"] == s["summary_upto"]
    assert mark["count"] > 0
    assert mark["at"] > 0
    # 摘要调用必须标成 summary，且走 _quick_chat（内部会自动切 light_model）
    assert calls["source"] == "summary"
    assert calls["max_tokens"] == context.SUMMARY_MAX_TOKENS


def test_compress_history_keeps_old_summary(monkeypatch):
    monkeypatch.setattr(context, "get_api_conf",
                        lambda p: ("k", "http://x", "main-model"))
    monkeypatch.setattr(context, "context_limits", lambda model="": (4000, 500))
    seen = {}

    def fake(api_key, api_url, api_model, messages, max_tokens=96, source="gal",
             stop_event=None):
        seen["prompt"] = messages[0]["content"]
        return "【摘要】\n合并后的摘要"

    monkeypatch.setattr(llm, "_quick_chat", fake)
    s = {"summary": "旧结论：保留 API 设计", "summary_upto": 2,
         "messages": [_msg("user", "字" * 100) for _ in range(20)]}
    assert context.compress_history(s) is True
    assert "旧结论：保留 API 设计" in seen["prompt"]
    assert s["summary_upto"] > 2
    # 旧摘要补一条兜底标记，本次压缩再追加一条
    assert len(s["compress_marks"]) == 2
    assert s["compress_marks"][-1]["upto"] == s["summary_upto"]


def test_compress_history_pins_task_goal_and_acceptance(monkeypatch):
    monkeypatch.setattr(context, "get_api_conf",
                        lambda p: ("k", "http://x", "main-model"))
    monkeypatch.setattr(context, "context_limits", lambda model="": (4000, 500))
    seen = {}

    def fake(api_key, api_url, api_model, messages, max_tokens=96, source="gal",
             stop_event=None):
        seen["prompt"] = messages[0]["content"]
        return "【摘要】\n合并后的摘要"

    monkeypatch.setattr(llm, "_quick_chat", fake)
    s = {"task_goal": {"text": "当前目标：做 A"},
         "acceptance": {"criteria": [{"text": "验收 A 通过"}]},
         "messages": [_msg("user", "字" * 100) for _ in range(20)]}
    assert context.compress_history(s) is True
    assert "当前任务目标（pin）" in seen["prompt"]
    assert "当前目标：做 A" in seen["prompt"]
    assert "验收项（pin）" in seen["prompt"] and "验收 A 通过" in seen["prompt"]
    assert "用户最初请求（pin）" not in seen["prompt"]


def test_restore_summary():
    s = {"summary": "abc", "summary_upto": 8, "summary_at": 123,
         "summary_mem": ["x"], "compress_marks": [{"at": 1, "index": 1,
                                                   "upto": 1, "count": 1}],
         "messages": [_msg("user", "hi")]}
    assert context.restore_summary(s) == 8
    assert s["summary"] == ""
    assert s["summary_upto"] == 0
    assert s["summary_at"] == 0
    assert s["summary_mem"] == []
    assert s["compress_marks"] == []
    assert context.restore_summary(s) == 0


def test_summary_info():
    s = {"summary": "摘要正文", "summary_upto": 6, "summary_at": 100,
         "messages": [_msg("user", "x") for _ in range(10)]}
    info = context.summary_info(s)
    assert info["compressed"] == 6
    assert info["remaining"] == 4
    assert info["total"] == 10
    assert info["chars"] == 4


def test_real_used_tokens_adds_increment():
    """事后真实 + 增量估算：基准取 usage，只有新增消息部分是估算。"""
    msgs = [_msg("user", "中文" * 50)]
    assert context.real_used_tokens(1000, msgs, 0, 1.0) > 1000
    assert context.real_used_tokens(1000, msgs, 1) == 1000     # 没有新增 → 只算基准
    assert context.real_used_tokens(0, msgs, 0) == 0           # 没拿到 usage → 交回调用方


def test_trim_tool_results():
    msgs = [_msg("system", "sys")]
    for _ in range(5):
        msgs.append(_msg("user", context.TOOL_MSG_PREFIX + "\n" + "x" * 3000))
    trimmed = context.trim_tool_results(msgs, 300)
    assert trimmed == 3                                 # 保留最近 2 条，清掉前 3 条
    assert "已折叠" in msgs[1]["content"]
    assert msgs[-1]["content"].count("x") == 3000        # 最近两条保持原文
    assert msgs[-2]["content"].count("x") == 3000


def test_trim_tool_results_keeps_search_sources():
    """搜索结果折叠时必须保留来源编号 + 标题，否则模型忘了自己搜过什么。"""
    body = ("【web_search】\n以下是根据用户问题「合成表」联网搜索到的参考资料。\n\n"
            "[1] 合成表 —— wiki\n正文甲\nURL: https://a.com/craft\n\n"
            "[2] 红石 —— wiki\n正文乙\nURL: https://a.com/redstone\n")
    msgs = [_msg("system", "sys")]
    for _ in range(4):
        msgs.append(_msg("user", context.TOOL_MSG_PREFIX + "\n" + body + "x" * 2000))
    trimmed = context.trim_tool_results(msgs, 300)
    assert trimmed >= 1
    folded = msgs[1]["content"]
    assert "已折叠" in folded
    assert "[1] 合成表" in folded
    assert "[2] 红石" in folded


def test_trim_history_to_budget():
    msgs = [_msg("system", "sys")] + [_msg("user", "x" * 2000) for _ in range(10)]
    out, dropped = context.trim_history_to_budget(msgs, 1000)
    assert dropped > 0
    assert out[0]["role"] == "system"
    assert "临时省略" in out[1]["content"]
    assert context.estimate_messages_tokens(out) <= 1000


def test_trim_history_keeps_when_fits():
    msgs = [_msg("system", "sys"), _msg("user", "hi")]
    out, dropped = context.trim_history_to_budget(msgs, 1000)
    assert dropped == 0
    assert out is msgs


def test_precompress_skips_without_sid():
    assert context.maybe_precompress("", 1000) is False


def test_session_context_exposes_compress_marks():
    """前端靠 context 事件同步分隔线，压缩标记必须随上下文一起回传。"""
    s = {"messages": [_msg("user", "hi")],
         "compress_marks": [{"at": 123, "index": 1, "upto": 1, "count": 1}]}
    ctx = context.session_context(s)
    assert ctx["compress_marks"] == s["compress_marks"]


def test_save_summary_merges_latest(monkeypatch):
    """压缩期间新增的消息不能被旧 data 覆盖"""
    old = [{"role": "user", "content": "第 %d 条" % i} for i in range(5)]
    disk = {"current": "s1", "sessions": [{
        "id": "s1", "summary": "",
        "messages": old + [{"role": "user", "content": "压缩期间的新消息"}],
    }]}
    monkeypatch.setattr(context, "load_sessions", lambda: disk)
    monkeypatch.setattr(context, "save_sessions", lambda data: None)
    src = {"summary": "摘要", "summary_upto": 5, "summary_at": 1,
           "summary_mem": ["要点"],
           "compress_marks": [{"at": 123, "index": 5, "upto": 5, "count": 5}],
           "messages": old}
    cur = context.save_summary("s1", src)
    assert cur["messages"] == old + [{"role": "user", "content": "压缩期间的新消息"}]
    assert cur["summary"] == "摘要"
    assert cur["summary_upto"] == 5
    assert cur["compress_marks"][0]["at"] == 123
    assert cur["compress_marks"][0]["index"] == 5
    assert cur["compress_marks"][0]["upto"] == 5


def test_save_summary_discards_when_range_edited(monkeypatch):
    """压缩期间「被压缩的那一段」被改写 → 摘要必须作废，不能写回错位内容"""
    old = [{"role": "user", "content": "原文 %d" % i} for i in range(5)]
    disk = {"current": "s1", "sessions": [{
        "id": "s1", "summary": "旧摘要", "summary_upto": 2,
        "messages": old,
    }]}
    monkeypatch.setattr(context, "load_sessions", lambda: disk)
    monkeypatch.setattr(context, "save_sessions", lambda data: None)
    edited = [dict(m) for m in old]
    edited[3]["content"] = "用户刚刚改过"
    src = {"summary": "新摘要", "summary_upto": 5, "summary_at": 1,
           "summary_mem": [], "messages": edited}
    assert context.save_summary("s1", src) is None
    assert disk["sessions"][0]["summary"] == "旧摘要"     # 磁盘没被写坏
    assert not src.get("summary")                          # 内存里的摘要已作废
    assert not src.get("compress_marks")


def test_flush_skips_when_ai_write_off(monkeypatch):
    disk = {"current": "s1", "sessions": [{
        "id": "s1", "ai_write": False, "summary_mem": ["要点"], "messages": []}]}
    monkeypatch.setattr(context, "load_sessions", lambda: disk)
    monkeypatch.setattr(context, "save_sessions", lambda data: None)
    called = []

    def fake_apply(*args, **kwargs):
        called.append(args)
        return {}, [], {}

    monkeypatch.setattr("app.knowledge.apply_memory_ops", fake_apply)
    assert context.flush_summary_memory("s1") == 0
    assert not called
    assert not disk["sessions"][0].get("summary_mem")


def test_flush_replaces_previous_summary_nodes(monkeypatch):
    """要点是「从累积摘要重新提炼的全量」→ 第二次压缩应替换上一批，而不是再追加（NOTE 43.87）"""
    disk = {"current": "s1", "sessions": [{
        "id": "s1", "ai_write": True, "summary_mem": ["新要点 A", "新要点 B"],
        "memory": {"nodes": [
            {"id": "h1", "title": "旧要点 1", "category": "历史摘要"},
            {"id": "h2", "title": "旧要点 2", "category": "历史摘要"},
            {"id": "k1", "title": "普通条目", "category": "结论"},
        ], "edges": []},
        "messages": []}]}
    monkeypatch.setattr(context, "load_sessions", lambda: disk)
    monkeypatch.setattr(context, "save_sessions", lambda data: None)
    seen = {}

    def fake_apply(sid, allowed, ops):
        seen["ops"] = ops
        return {"delete": 2, "add": 2}, [], {}

    monkeypatch.setattr("app.knowledge.apply_memory_ops", fake_apply)
    assert context.flush_summary_memory("s1") == 4
    ops = seen["ops"]
    assert [o["id"] for o in ops if o["op"] == "delete"] == ["h1", "h2"]
    assert all(o["category"] == "历史摘要" for o in ops if o["op"] == "add")
    assert "k1" not in [o.get("id") for o in ops if o["op"] == "delete"]


def test_begin_compress_blocks_concurrent_run():
    """后台预压缩 / 请求内同步压缩 / 手动压缩共用同一个闸门"""
    sid = "s-busy-test"
    assert context.begin_compress(sid) is True
    assert context.begin_compress(sid) is False
    assert context.compress_busy(sid) is True
    context.end_compress(sid)
    assert context.compress_busy(sid) is False


def test_invalidate_compression_clears_summary_and_marks():
    s = {"summary": "旧摘要", "summary_upto": 5, "summary_at": 1,
         "summary_mem": ["要点"],
         "compress_marks": [{"at": 1, "index": 5, "upto": 5, "count": 5}]}
    assert invalidate_compression(s) == 5
    assert s["summary"] == ""
    assert s["summary_upto"] == 0
    assert s["summary_at"] == 0
    assert s["summary_mem"] == []
    assert s["compress_marks"] == []


def test_input_breakdown_splits_system_and_roles():
    sys_text = "S" * 100
    msgs = [
        _msg("system", sys_text),
        _msg("user", "U" * 10),
        _msg("assistant", "A" * 20),
        _msg("user", context.TOOL_MSG_PREFIX + "T" * 30),
    ]
    parts = context.input_breakdown(
        msgs, sys_parts={"rules": 40, "tools_doc": 10, "memory": 20},
        summary_chars=20, schema_chars=50)
    assert parts["rules"] == 40
    assert parts["tools_doc"] == 10
    assert parts["memory"] == 20
    assert parts["summary"] == 20
    # 系统提示 100 字里已知 90 字（rules/tools_doc/memory/summary），其余算 other_sys
    assert parts["other_sys"] == 10
    assert parts["user"] == 10
    assert parts["reply"] == 20
    assert parts["tool_result"] == len(context.TOOL_MSG_PREFIX) + 30
    assert parts["schema"] == 50


def test_input_breakdown_clamps_when_parts_exceed_system():
    """子块长度加起来超过系统提示（重拼改了内容）时不能出现负数。"""
    parts = context.input_breakdown(
        [_msg("system", "x" * 10)],
        sys_parts={"rules": 100, "tools_doc": 100, "memory": 100})
    assert parts["other_sys"] == 0


def test_scale_parts_sums_to_real_prompt():
    parts = {"rules": 40, "memory": 60, "tool_result": 100}
    out = context.scale_parts(parts, 400, 200)
    assert out["rules"] == 80 and out["memory"] == 120 and out["tool_result"] == 200
    assert sum(out.values()) == 400
    assert context.scale_parts(parts, 0, 200) is None
    assert context.scale_parts(parts, 400, 0) is None


def test_slim_rounds_detail_keeps_in_parts():
    from app.api.chat import _slim_rounds_detail
    rows = [{"n": 1, "prompt": 10, "completion": 2, "state": "done",
             "in_parts": {"rules": 6, "memory": 4}}]
    out = _slim_rounds_detail(rows)
    assert out[0]["in_parts"] == {"rules": 6, "memory": 4}
    assert _slim_rounds_detail([{"n": 2, "prompt": 10}])[0]["in_parts"] is None


def _asst(proc, final):
    return {"role": "assistant", "content": proc + "\n\n" + final,
            "blocks": [{"kind": "text", "text": proc},
                       {"kind": "tool", "i": 0},
                       {"kind": "text", "text": final}]}


def test_history_process_compaction_keeps_recent_turns():
    """B档 B1：旧回合只留「过程摘要 + 最后一段答案」，最近 2 条原文保留。"""
    s = {"messages": [
        {"role": "user", "content": "u1"}, _asst("过程一" * 100, "答案一"),
        {"role": "user", "content": "u2"}, _asst("过程二" * 100, "答案二"),
        {"role": "user", "content": "u3"}, _asst("过程三" * 100, "答案三"),
        {"role": "user", "content": "u4"},
    ]}
    msgs = context.build_context_messages(s, "SYS")
    asst = [m["content"] for m in msgs if m["role"] == "assistant"]
    assert asst[0].startswith("【本回合过程摘要】")
    assert asst[0].endswith("答案一")                 # 最后一段答案原样保留
    assert "过程一" * 100 not in asst[0]              # 过程全文不在了
    assert asst[1] == "过程二" * 100 + "\n\n答案二"    # 最近 2 条不动
    assert asst[2] == "过程三" * 100 + "\n\n答案三"


def test_history_process_compaction_skips_unreliable_messages(monkeypatch):
    """没有 blocks / blocks 覆盖不全时保持原文，避免丢内容。"""
    s = {"messages": [{"role": "assistant", "content": "a\n\nb"}]}
    assert context.build_context_messages(s, "S")[1]["content"] == "a\n\nb"
    partial = {"role": "assistant", "content": "很长的正文" * 100,
               "blocks": [{"kind": "text", "text": "短过程"},
                          {"kind": "text", "text": "短答案"}]}
    assert (context.build_context_messages({"messages": [partial]}, "S")[1]["content"]
            == partial["content"])
    # 关闭开关时整段保留
    monkeypatch.setattr(context, "load_config",
                        lambda: {"history_process_keep_turns": 0})
    s2 = {"messages": [_asst("过程" * 100, "答案"), _asst("过程2", "答案2")]}
    out = context.build_context_messages(s2, "S")
    assert out[1]["content"].startswith("过程")
