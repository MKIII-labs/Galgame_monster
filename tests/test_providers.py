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


def test_split_system_converts_images_to_anthropic_blocks():
    """原生视觉：OpenAI 的 image_url 必须换成 Anthropic 的 base64 image 块。"""
    body = providers.anthropic_body("claude-x", [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "text", "text": "看图"},
        ]},
    ], 100)
    parts = body["messages"][0]["content"]
    assert parts[0] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "AAAA"}}
    assert parts[1] == {"type": "text", "text": "看图"}


def test_split_system_keeps_plain_string_content():
    body = providers.anthropic_body("claude-x", [{"role": "user", "content": "hi"}], 10)
    assert body["messages"][0]["content"] == "hi"


def test_anthropic_body_converts_tools():
    body = providers.anthropic_body(
        "claude-x", [{"role": "user", "content": "hi"}], 10,
        tools=[{"type": "function", "function": {
            "name": "read_file", "description": "读文件",
            "parameters": {"type": "object", "properties": {}}}}])
    assert body["tools"][0]["name"] == "read_file"
    assert body["tools"][0]["input_schema"]["type"] == "object"
    assert "function" not in body["tools"][0]


def test_anthropic_tool_event_stream():
    start = providers.anthropic_tool_event({
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "id": "tu_1", "name": "read_file"}})
    assert start["kind"] == "start" and start["name"] == "read_file"
    delta = providers.anthropic_tool_event({
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"path":"a.py"}'}})
    assert delta["kind"] == "delta" and delta["json"] == '{"path":"a.py"}'
    # 文本块不是工具事件
    assert providers.anthropic_tool_event({
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}}) is None
