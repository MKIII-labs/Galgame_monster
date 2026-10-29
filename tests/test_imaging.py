# -*- coding: utf-8 -*-
"""多模态 content 构建：单图兼容 + 多图。"""
import json


def _fake_post(seen):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def post(url, headers=None, data=None, timeout=None):
        seen["body"] = json.loads(data)
        return Resp()

    return post


def test_chat_vision_disables_thinking(monkeypatch):
    """聊天模型识图必须关思考：否则 reasoning 会吃光 max_tokens，content 为空。"""
    from app import imaging

    seen = {}
    monkeypatch.setattr(imaging, "get_api_conf",
                        lambda p: ("k", "https://api.deepseek.com/v1/chat/completions", "m"))
    monkeypatch.setattr(imaging, "effective_think_mode", lambda url: "deepseek")
    monkeypatch.setattr(imaging, "load_config", lambda: {"chat_think_level": "high"})
    monkeypatch.setattr(imaging.requests, "post", _fake_post(seen))

    assert imaging._recognize_with("chat", "image/jpeg", "AAA",
                                   question="看看", max_tokens=100) == "ok"
    assert seen["body"]["thinking"] == {"type": "disabled"}


def test_vision_api_does_not_send_thinking(monkeypatch):
    from app import imaging

    seen = {}
    monkeypatch.setattr(imaging, "get_api_conf", lambda p: ("k", "http://x/v1/chat/completions", "m"))
    monkeypatch.setattr(imaging, "get_api_protocol", lambda p: "openai")
    monkeypatch.setattr(imaging.requests, "post", _fake_post(seen))

    assert imaging._recognize_with("vision", "image/jpeg", "AAA",
                                   question="看看", max_tokens=100) == "ok"
    assert "thinking" not in seen["body"]


def test_build_vision_content_single_image_backward_compatible():
    from app.imaging import build_vision_content
    parts = build_vision_content("这是什么", "image/png", "AAA")
    assert parts == [
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "text", "text": "这是什么"},
    ]


def test_build_vision_content_supports_multiple_images():
    from app.imaging import build_vision_content
    parts = build_vision_content("对比这两张", "image/png", "AAA", images=[
        {"mime": "image/png", "b64": "AAA"},
        {"mime": "image/jpeg", "b64": "BBB"},
    ])
    urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert urls == [
        "data:image/png;base64,AAA",
        "data:image/jpeg;base64,BBB",
    ]
    assert parts[-1] == {"type": "text", "text": "对比这两张"}
