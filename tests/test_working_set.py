# -*- coding: utf-8 -*-
"""工作集（跨回合历史原文预算）与压缩切点保护。"""

from app import context
from app.context import TOOL_MSG_PREFIX


def _cfg(**kw):
    base = {"token_profile": "balanced", "history_working_set_tokens": 40000}
    base.update(kw)
    return base


def test_working_set_off_in_quality_mode(monkeypatch):
    monkeypatch.setattr(context, "load_config",
                        lambda: _cfg(token_profile="quality"))
    assert context.working_set_tokens() == 0


def test_working_set_reads_config(monkeypatch):
    monkeypatch.setattr(context, "load_config",
                        lambda: _cfg(history_working_set_tokens=12000))
    assert context.working_set_tokens() == 12000


def test_working_set_zero_means_off(monkeypatch):
    monkeypatch.setattr(context, "load_config",
                        lambda: _cfg(history_working_set_tokens=0))
    assert context.working_set_tokens() == 0


def test_manual_keep_budget_capped_by_working_set(monkeypatch):
    monkeypatch.setattr(context, "load_config",
                        lambda: _cfg(history_working_set_tokens=200))
    msgs = [{"role": "user", "content": "字" * 4000}]      # ≈ 2400 token
    assert context.manual_keep_budget(msgs, 0, "") == 200


def test_manual_keep_budget_never_exceeds_history(monkeypatch):
    monkeypatch.setattr(context, "load_config",
                        lambda: _cfg(history_working_set_tokens=100000))
    msgs = [{"role": "user", "content": "字" * 100}]        # ≈ 60 token
    assert context.manual_keep_budget(msgs, 0, "") == \
        context._context_text_tokens("", "", msgs)


def test_history_tokens_respects_summary_cut(monkeypatch):
    monkeypatch.setattr(context, "load_config", lambda: _cfg(token_profile="quality"))
    msgs = [{"role": "user", "content": "字" * 600},
            {"role": "assistant", "content": "字" * 600}]
    full = context.history_tokens({"messages": msgs})
    cut = context.history_tokens({"messages": msgs, "summary_upto": 1})
    assert 0 < cut < full


def test_clamp_keeps_current_turn(monkeypatch):
    msgs = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "答一"},
        {"role": "user", "content": "第二轮"},
        {"role": "assistant", "content": "答二"},
    ]
    # 切点被推到当前回合内部（3）时，拉回到当前回合起点（2）
    assert context._clamp_keep_from(msgs, 0, 3) == 2
    assert context._clamp_keep_from(msgs, 0, 2) == 2
    assert context._clamp_keep_from(msgs, 0, 1) == 1


def test_clamp_ignores_tool_messages():
    msgs = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "调用工具"},
        {"role": "user", "content": TOOL_MSG_PREFIX + "\n结果"},
        {"role": "user", "content": "第二轮"},
        {"role": "assistant", "content": "答"},
    ]
    # 最后一条「真正的用户消息」是 index 3，工具结果不算新回合
    assert context._clamp_keep_from(msgs, 0, 4) == 3


def test_clamp_noop_when_all_in_summary():
    msgs = [{"role": "assistant", "content": "旧"}]
    assert context._clamp_keep_from(msgs, 1, 5) == 5

def test_persist_keeps_newer_summary():
    """后台压缩推进的摘要，不能被请求收尾时的旧快照覆盖回去。"""
    import app.api.chat as chat

    s = {"id": "x", "summary": "旧摘要", "summary_upto": 10,
         "messages": [{"role": "user", "content": "本轮"}]}
    fresh = {"id": "x", "summary": "新摘要", "summary_upto": 20,
             "summary_at": 1, "compress_marks": [1]}
    out = chat._keep_fresh_summary(s, fresh)
    assert out["summary"] == "新摘要"
    assert out["summary_upto"] == 20
    assert out["messages"] == [{"role": "user", "content": "本轮"}]

    # 自己手里的更新（例如本轮又压了一次）时不要被盘上的旧数据盖掉
    s2 = {"summary": "较新", "summary_upto": 30}
    fresh2 = {"summary": "较旧", "summary_upto": 20}
    assert chat._keep_fresh_summary(s2, fresh2)["summary"] == "较新"


def test_assistant_keep_set_waterline():
    """原文保留区按 batch 推进：边界之间只追加，不会每轮压掉一条旧消息。"""
    msgs = [{"role": "assistant", "content": "a"} for _ in range(12)]
    # step=4：4 条时边界为 4，只保留最近 2 条
    assert context._assistant_keep_set(msgs[:4], 2, 4) == {2, 3}
    # 5~7 条：边界仍是 4，已保留的 2/3 不动，新消息继续加入
    assert context._assistant_keep_set(msgs[:5], 2, 4) == {2, 3, 4}
    assert context._assistant_keep_set(msgs[:7], 2, 4) == {2, 3, 4, 5, 6}
    # 到 8 条才整体推进一次（批量压 2~5）
    assert context._assistant_keep_set(msgs[:8], 2, 4) == {6, 7}
    # step=1 = 旧行为：最近 keep 条
    assert context._assistant_keep_set(msgs[:5], 2, 1) == {3, 4}
