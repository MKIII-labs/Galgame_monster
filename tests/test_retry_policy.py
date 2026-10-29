# -*- coding: utf-8 -*-
"""重试策略与上下文预算（对齐 dsh retryPolicy / compaction 阈值）。"""
from app import context
from app.api import chat


def test_retry_mode_per_provider():
    cfg = {"chat_retry_mode": "always", "chat_presets": {
        "backup": {"retry_mode": "normal"},
    }}
    assert chat._retry_mode_for(cfg, "main") == "always"
    assert chat._retry_mode_for(cfg, "backup") == "normal"
    assert chat._retry_mode_for({"chat_retry_mode": "bogus"}, "main") == "normal"
    assert chat._retry_mode_for({}, "main") == "normal"


def test_retry_delay_bounds():
    cfg = {"chat_retry_backoff_ms": 500, "chat_retry_backoff_max_ms": 10000,
           "chat_transport_backoff_ms": 5000,
           "chat_transport_backoff_max_ms": 30000,
           "chat_retry_jitter": 0.1}
    normal1 = chat._retry_delay("server", 1, cfg)
    assert 0.45 <= normal1 <= 0.56
    normal_late = chat._retry_delay("server", 20, cfg)
    assert normal_late <= 11.0
    transport1 = chat._retry_delay("upload", 1, cfg)
    assert 4.5 <= transport1 <= 5.6
    transport_late = chat._retry_delay("network", 20, cfg)
    assert transport_late <= 33.0
    # 上游 Retry-After 有效且不超上限 → 直接采用，不加抖动
    assert chat._retry_delay("rate_limit", 1, cfg, retry_after=3) == 3.0
    # 超过上限 → 不用它，退回本地退避
    assert chat._retry_delay("rate_limit", 1, cfg, retry_after=99) <= 0.56


def test_always_mode_is_unbounded_but_safe():
    idle = {"visible": "", "tool_calls": [], "stream_state": "idle"}
    assert chat._should_retry_round(idle, 999, 2, mode="always") is True
    # 0 仍然是显式关闭
    assert chat._should_retry_round(idle, 0, 0, mode="always") is False
    # 安全阀到点就停
    assert chat._should_retry_round(idle, 999, 2, mode="always",
                                    deadline=10.0, now=11.0) is False
    # 余额不足永不无限重试
    assert chat._should_retry_round(
        {"visible": "", "tool_calls": [], "stream_state": "billing"},
        0, 2, mode="always") is False
    # 空响应只按普通预算试几次，避免无限烧钱
    empty = {"visible": "", "tool_calls": [], "stream_state": "empty"}
    assert chat._should_retry_round(empty, 1, 2, mode="always") is True
    assert chat._should_retry_round(empty, 2, 2, mode="always") is False
    # 失败尝试带工具调用时不重试（llm 会清空，这里再挡一道）
    assert chat._should_retry_round(
        {"visible": "", "tool_calls": [{"tool": "write_file"}],
         "stream_state": "idle"}, 0, 2, mode="always") is False


def test_effective_output_cap_and_request_budget(monkeypatch):
    cfg = {"chat_max_tokens": 65536, "chat_output_window_ratio": 0.25,
           "chat_output_headroom": 8192, "compress_ratio": 0.8}
    monkeypatch.setattr(context, "load_config", lambda: cfg)
    monkeypatch.setattr(context, "context_limits", lambda model="": (128000, 4096))

    # 65k 输出被夹到窗口的 25%
    assert context.effective_output_cap(128000, cfg) == 32000
    # 预算 = min(128k*0.8, 128k - 32k - 8k) = 87808
    assert context.request_budget() == 87808

    # 小窗口不能把保底算成大于窗口
    small = {"chat_max_tokens": 65536, "chat_output_window_ratio": 0.25,
             "chat_output_headroom": 8192, "compress_ratio": 0.8}
    assert context.effective_output_cap(4000, small) == 1024
    monkeypatch.setattr(context, "context_limits", lambda model="": (4000, 500))
    assert 0 < context.request_budget() <= 2000


def test_retryable_failure_excludes_user_stop():
    assert chat._retryable_failure({"stream_state": "idle"}) is True
    assert chat._retryable_failure({"stream_state": "idle", "stopped": True}) is False
    assert chat._retryable_failure({"stream_state": "auth"}) is False
    assert chat._retryable_failure({"stream_state": "empty"}) is True
