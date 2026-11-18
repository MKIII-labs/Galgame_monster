# -*- coding: utf-8 -*-
"""输入预检 / 润色：确认新提示词与返回字段（gaps / example / rewrite）没丢。"""
from app.api import misc


class _H:
    def __init__(self, body):
        self._incoming = body
        self.calls = []

    def _body(self):
        return self._incoming

    def _json(self, obj, code=200):
        self.calls.append((code, obj))
        return obj

    def last(self):
        return self.calls[-1]


def _patch(monkeypatch, reply):
    seen = {}

    def fake_quick_chat(key, url, model, messages, **kw):
        seen["prompt"] = messages[0]["content"]
        return reply

    monkeypatch.setattr(misc, "get_api_conf", lambda *a, **k: ("k", "http://x", "m"))
    monkeypatch.setattr(misc.llm, "_quick_chat", fake_quick_chat)
    monkeypatch.setattr(misc.expression, "recent", lambda n=3: [])
    return seen


def test_check_returns_example_and_rewrite(monkeypatch):
    seen = _patch(monkeypatch, '{"gaps": ["没说是哪个文件", "期望产出不清"],'
                               ' "example": "改一下 web/chat.js 的发送逻辑",'
                               ' "rewrite": "改 web/chat.js 的发送逻辑（待确认：改成什么样）"}')
    h = _H({"text": "帮我改一下那个发送的东西", "action": "check"})
    misc._api_compose(h)
    code, obj = h.last()
    assert code == 200 and obj["ok"] is True
    assert obj["gaps"] == ["没说是哪个文件", "期望产出不清"]
    assert "web/chat.js" in obj["example"]
    assert "待确认" in obj["rewrite"]
    # 提示词要覆盖「指代 / 上下文 / 期望产出」的判断维度，并要求 rewrite
    assert "指代" in seen["prompt"] and "上下文" in seen["prompt"]
    assert '"rewrite"' in seen["prompt"]


def test_polish_asks_to_keep_intent_and_mark_unknowns(monkeypatch):
    seen = _patch(monkeypatch, '{"intent": "代码", "text": "改 web/chat.js 的发送逻辑", "gaps": []}')
    h = _H({"text": "改一下那个发送", "action": "polish"})
    misc._api_compose(h)
    code, obj = h.last()
    assert code == 200 and obj["text"] == "改 web/chat.js 的发送逻辑"
    # 新规则：可以专业化补全，但必须保留原意、不编事实、未知写「待确认」
    assert "保留原意" in seen["prompt"]
    assert "待确认" in seen["prompt"]


def test_check_keeps_untouched_when_model_returns_garbage(monkeypatch):
    _patch(monkeypatch, "这不是 JSON")
    h = _H({"text": "帮我改一下那个发送的东西", "action": "check"})
    misc._api_compose(h)
    code, obj = h.last()
    # 预检失败必须静默：ok=True + 空建议，绝不弹错误
    assert code == 200 and obj["ok"] is True
    assert obj["gaps"] == [] and obj["rewrite"] == ""


def test_bad_action_rejected():
    h = _H({"text": "随便写点什么", "action": "nope"})
    misc._api_compose(h)
    code, obj = h.last()
    assert code == 200 and obj["ok"] is False and obj["error"] == "bad_action"
