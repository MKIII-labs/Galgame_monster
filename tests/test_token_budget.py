# -*- coding: utf-8 -*-
"""预算快照：必须与线上实际使用的 context 计算逐值一致。"""

from app import context, token_budget


CFG = {
    "chat_context_limit": 1_000_000,
    "chat_reserve_output": 16384,
    "chat_max_tokens": 65536,
    "chat_output_window_ratio": 0.25,
    "chat_output_headroom": 8192,
    "compress_ratio": 0.8,
}


def _patch(monkeypatch, cfg):
    monkeypatch.setattr(context, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(context, "get_api_conf", lambda p: ("k", "http://x", "m"))


def test_budget_matches_existing_calculations(monkeypatch):
    _patch(monkeypatch, CFG)
    b = token_budget.budget("m")
    assert b.window == 1_000_000
    assert b.reserve == 16384
    # 这两个值必须与真正发请求时用的完全一致，否则观测就是假的
    assert b.prompt_cap == context.request_budget("m")
    assert b.output_cap == context.effective_output_cap(1_000_000, CFG)
    assert b.unlimited is False


def test_level_thresholds(monkeypatch):
    _patch(monkeypatch, CFG)
    b = token_budget.budget("m")
    assert token_budget.level(0, b) == 0
    assert token_budget.level(b.l1_fold - 1, b) == 0
    assert token_budget.level(b.l1_fold, b) == 1
    assert token_budget.level(b.l2_micro, b) == 2
    assert token_budget.level(b.l3_compact, b) == 3


def test_unlimited_window_disables_levels(monkeypatch):
    _patch(monkeypatch, {"chat_context_limit": 0})
    b = token_budget.budget("m")
    assert b.unlimited is True
    assert b.prompt_cap == 0
    assert token_budget.level(10 ** 9, b) == 0


def test_as_dict_is_json_safe(monkeypatch):
    _patch(monkeypatch, CFG)
    d = token_budget.budget("m").as_dict()
    assert set(d) == {"window", "reserve", "headroom", "output_cap", "prompt_cap",
                      "l1_fold", "l2_micro", "l3_compact"}
    assert all(isinstance(v, int) for v in d.values())
