# -*- coding: utf-8 -*-
"""多 provider 管理：故障转移链、按模型的上下文窗口、每日预算统计。"""
import time

from app import config, usage


def _cfg(**kw):
    base = {"chat_url": "https://api.deepseek.com/v1", "chat_model": "deepseek-chat",
            "chat_api_key": "k1", "chat_kind": "openai",
            "chat_context_limit": 0, "model_contexts": {},
            "chat_presets": {}, "chat_fallback": []}
    base.update(kw)
    return base


def test_chain_starts_with_main():
    chain = config.provider_chain(_cfg())
    assert chain[0][0] == "main"
    assert chain[0][1] == "openai"
    assert chain[0][3].endswith("/chat/completions")


def test_chain_adds_fallbacks_in_order_and_detects_kind():
    cfg = _cfg(chat_presets={
        "backup": {"url": "https://api.anthropic.com", "key": "k2", "model": "claude-x"},
        "local": {"url": "http://127.0.0.1:11434/v1", "model": "qwen"},
    }, chat_fallback=["backup", "local", "missing"])
    chain = config.provider_chain(cfg)
    assert [c[0] for c in chain] == ["main", "backup", "local"]
    kinds = {c[0]: c[1] for c in chain}
    assert kinds["backup"] == "anthropic"
    assert kinds["local"] == "openai"


def test_chain_skips_incomplete_presets():
    cfg = _cfg(chat_presets={"broken": {"url": "", "model": ""}},
               chat_fallback=["broken"])
    assert len(config.provider_chain(cfg)) == 1


def test_context_limit_ignores_model_tables():
    """不做前缀匹配：model_contexts 已废弃，填了也不参与计算。"""
    cfg = _cfg(model_contexts={"claude": 200000, "deepseek": 64000})
    assert config.model_context_limit("claude-3-opus", cfg) == 0
    assert config.model_context_limit("deepseek-chat", cfg) == 0
    assert config.model_context_limit("unknown-model", cfg) == 0


def test_context_limit_zero_means_unlimited():
    assert config.model_context_limit("whatever", _cfg()) == 0
    assert config.model_context_limit("", _cfg(chat_context_limit=0)) == 0


def test_explicit_context_limit_used_as_is():
    assert config.model_context_limit("x", _cfg(chat_context_limit=1000000)) == 1000000
    assert config.model_context_limit("x", _cfg(chat_context_limit=64000)) == 64000
    # 不再有 1M 防呆上限：用户填多少就是多少
    assert config.model_context_limit("x", _cfg(chat_context_limit=2000000)) == 2000000


def test_context_limits_clamps_reserve(monkeypatch):
    from app import context as ctx_mod
    monkeypatch.setattr(ctx_mod, "load_config", lambda: {
        "chat_context_limit": 1000000, "chat_reserve_output": 3000320})
    limit, reserve = ctx_mod.context_limits("deepseek-flash")
    assert limit == 1000000
    assert reserve == config.MAX_RESERVE


def test_context_limits_unlimited_has_no_budget(monkeypatch):
    """没填窗口 = 真无限：不限预算、不触发压缩。"""
    from app import context as ctx_mod
    monkeypatch.setattr(ctx_mod, "load_config",
                        lambda: {"chat_context_limit": 0, "chat_reserve_output": 16384})
    assert ctx_mod.context_limits("deepseek-flash") == (0, 0)
    assert ctx_mod.request_budget("deepseek-flash") == 0
    # 输出上限回落到 chat_max_tokens（默认 8192），不再按窗口比例算
    assert ctx_mod.effective_output_cap(0, {"chat_max_tokens": 8192}) == 8192


def test_today_total_sums_today_only(monkeypatch):
    now = time.time()
    rows = [{"ts": now, "total": 100}, {"ts": now - 86400 * 2, "total": 999}]
    monkeypatch.setattr(usage, "load_usage", lambda: rows)
    monkeypatch.setattr(usage, "_TODAY_CACHE", {"at": 0.0, "total": 0})
    assert usage.today_total(ttl=0) == 100
