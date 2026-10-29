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


def test_context_limit_uses_longest_prefix():
    cfg = _cfg(model_contexts={"claude": 200000, "claude-3": 100000,
                               "deepseek": 64000})
    assert config.model_context_limit("claude-3-opus", cfg) == 100000
    assert config.model_context_limit("claude-sonnet", cfg) == 200000
    assert config.model_context_limit("deepseek-chat", cfg) == 64000
    assert config.model_context_limit("unknown-model", cfg) == 128000


def test_context_limit_clamps_absurd_config():
    # 用户填 1 亿时钳到防呆上限 1M（而不是像以前那样被静默忽略成 128K）
    cfg = _cfg(chat_context_limit=100000000, model_contexts={})
    assert config.model_context_limit("deepseek-flash", cfg) == config.MAX_CONTEXT_HARD
    assert config.model_context_limit("totally-unknown", cfg) == config.MAX_CONTEXT_HARD


def test_explicit_context_limit_overrides_builtin_both_ways():
    """显式填窗口 = 用户说了算：既能放宽到 1M，也能收紧到下层的值。"""
    assert config.model_context_limit("deepseek-flash", _cfg()) == 1000000
    assert config.model_context_limit("deepseek-chat", _cfg()) == 128000
    assert config.model_context_limit(
        "deepseek-flash", _cfg(chat_context_limit=1000000)) == 1000000
    assert config.model_context_limit(
        "deepseek-flash", _cfg(chat_context_limit=64000)) == 64000
    # 比防呆上限还大 → 钳住，不能出现「进度环 1 亿」这种假窗口
    assert config.model_context_limit(
        "deepseek-flash", _cfg(chat_context_limit=100_000_000)) == config.MAX_CONTEXT_HARD


def test_context_limits_clamps_reserve(monkeypatch):
    from app import context as ctx_mod
    monkeypatch.setattr(ctx_mod, "load_config", lambda: {
        "chat_context_limit": 100000000, "chat_reserve_output": 3000320,
        "model_contexts": {}})
    limit, reserve = ctx_mod.context_limits("deepseek-flash")
    assert limit == config.MAX_CONTEXT_HARD
    assert reserve == config.MAX_RESERVE


def test_today_total_sums_today_only(monkeypatch):
    now = time.time()
    rows = [{"ts": now, "total": 100}, {"ts": now - 86400 * 2, "total": 999}]
    monkeypatch.setattr(usage, "load_usage", lambda: rows)
    monkeypatch.setattr(usage, "_TODAY_CACHE", {"at": 0.0, "total": 0})
    assert usage.today_total(ttl=0) == 100
