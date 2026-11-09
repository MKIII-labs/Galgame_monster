# -*- coding: utf-8 -*-
"""Anthropic 提示缓存：只在开启时给稳定段与对话末尾打 cache_control。"""

from app import providers

MSGS = [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": "第一问"},
    {"role": "assistant", "content": "第一答"},
    {"role": "user", "content": "第二问"},
]


def test_cache_off_keeps_plain_body():
    body = providers.anthropic_body("m", MSGS, stream=False)
    assert body["system"] == "系统提示"
    assert body["messages"][-1]["content"] == "第二问"


def test_cache_on_marks_system_and_last_message():
    body = providers.anthropic_body("m", MSGS, stream=False, cache=True)
    assert isinstance(body["system"], list)
    assert body["system"][0]["text"] == "系统提示"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    last = body["messages"][-1]["content"]
    assert isinstance(last, list)
    assert last[-1]["cache_control"] == {"type": "ephemeral"}
    # 更早的消息保持原样（缓存断点只打一个）
    assert body["messages"][0]["content"] == "第一问"


def test_cache_on_multimodal_last_block():
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        {"type": "text", "text": "看图"}]}]
    body = providers.anthropic_body("m", msgs, stream=False, cache=True)
    blocks = body["messages"][-1]["content"]
    assert blocks[-1]["type"] == "text"
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    # 不能改动调用方传进来的消息对象
    assert "cache_control" not in msgs[0]["content"][-1]


def test_cache_on_without_system():
    body = providers.anthropic_body("m", [{"role": "user", "content": "hi"}],
                                    stream=False, cache=True)
    assert "system" not in body
    assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_cache_off_keeps_all_content_strings():
    body = providers.anthropic_body("m", MSGS, stream=False)
    assert all(isinstance(m["content"], str) for m in body["messages"])
