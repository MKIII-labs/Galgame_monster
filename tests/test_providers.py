# -*- coding: utf-8 -*-
"""多服务商协议适配：kind 判定、Anthropic 请求体与流事件。"""
from app import providers


def test_kind_from_url_and_explicit():
    assert providers.kind_of("https://api.anthropic.com") == "anthropic"
    assert providers.kind_of("https://api.deepseek.com/v1") == "openai"
    assert providers.kind_of("https://x.example.com", "anthropic") == "anthropic"
    # 显式配置优先于地址猜测
    assert providers.kind_of("https://api.anthropic.com", "openai") == "openai"


def test_anthropic_url_completion():
    assert providers.anthropic_url("https://api.anthropic.com").endswith("/v1/messages")
    assert providers.anthropic_url("https://api.anthropic.com/v1").endswith("/v1/messages")
    assert providers.anthropic_url("https://api.anthropic.com/v1/messages").endswith(
        "/v1/messages")


def test_body_extracts_system_and_keeps_messages():
    body = providers.anthropic_body("claude-x", [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "你好"},
    ], 100)
    assert body["system"] == "规则"
    assert body["messages"] == [{"role": "user", "content": "你好"}]
    assert body["max_tokens"] == 100 and body["stream"] is True


def test_body_has_no_system_key_when_absent():
    body = providers.anthropic_body("claude-x", [{"role": "user", "content": "hi"}], 0)
    assert "system" not in body
    assert body["max_tokens"] == providers.DEFAULT_MAX_TOKENS


def test_delta_text():
    assert providers.delta({"type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "hi"}}) == (
        "hi", "", None, None)
    assert providers.delta({"type": "content_block_delta",
                            "delta": {"type": "thinking_delta", "thinking": "hm"}}) == (
        "", "hm", None, None)


def test_delta_finish_reason_mapping():
    out = providers.delta({"type": "message_delta",
                           "delta": {"stop_reason": "max_tokens"},
                           "usage": {"output_tokens": 5}})
    assert out[2] == "length"
    out2 = providers.delta({"type": "message_delta",
                            "delta": {"stop_reason": "end_turn"}})
    assert out2[2] == "stop"


def test_usage_mapping():
    assert providers.usage_of({"input_tokens": 10, "output_tokens": 4}) == {
        "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
    assert providers.usage_of({}) == {}


def test_text_of_non_stream():
    data = {"content": [{"type": "text", "text": "a"},
                        {"type": "thinking", "thinking": "x"},
                        {"type": "text", "text": "b"}]}
    assert providers.text_of(data) == "ab"
