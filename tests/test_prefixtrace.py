# -*- coding: utf-8 -*-
"""前缀观测：判断「从第 0 个 token 起」还剩多少可复用前缀。"""

from app import prefixtrace


SYS = {"role": "system", "content": "系统提示"}
U1 = {"role": "user", "content": "你好"}
A1 = {"role": "assistant", "content": "在"}


def setup_function():
    prefixtrace.reset()


def test_first_request_is_cold():
    out = prefixtrace.observe("s1", [SYS, U1])
    assert out["cold"] is True
    assert out["prefix_msgs"] == 0


def test_append_only_reuses_full_prefix():
    msgs = [SYS, U1]
    prefixtrace.observe("s1", msgs)
    out = prefixtrace.observe("s1", msgs + [A1])
    assert out["cold"] is False
    assert out["prefix_msgs"] == 2
    assert out["prefix_ratio"] == 1.0
    assert out["sys_changed"] is False
    assert out["first_divergence"] is None
    assert out["prefix_tokens"] > 0


def test_system_change_breaks_prefix_at_zero():
    prefixtrace.observe("s1", [SYS, U1])
    out = prefixtrace.observe("s1", [{"role": "system", "content": "系统提示改了"}, U1])
    assert out["sys_changed"] is True
    assert out["prefix_msgs"] == 0
    assert out["first_divergence"] == 0
    assert out["prefix_ratio"] == 0.0


def test_history_rewrite_reports_divergence_index():
    prefixtrace.observe("s1", [SYS, U1, A1])
    out = prefixtrace.observe("s1", [SYS, U1, {"role": "assistant", "content": "改写过"}])
    assert out["prefix_msgs"] == 2
    assert out["first_divergence"] == 2
    assert out["sys_changed"] is False


def test_sessions_are_isolated():
    prefixtrace.observe("a", [U1])
    out = prefixtrace.observe("b", [U1])
    assert out["cold"] is True


def test_content_list_is_supported():
    first = [U1, {"role": "user", "content": [{"type": "text", "text": "看图"}]}]
    prefixtrace.observe("s1", [SYS] + first)
    out = prefixtrace.observe("s1", [SYS] + first + [A1])
    assert out["prefix_msgs"] == 3
