# -*- coding: utf-8 -*-
"""对话入口的边界行为：不装模型，只锁请求校验与租约语义。"""
from app.api import chat


class _H:
    def __init__(self, body):
        self.body = body
        self.out = None

    def _body(self):
        return self.body

    def _json(self, obj, code=200):
        self.out = (code, obj)
        return self.out


def test_api_chat_requires_session_id():
    h = _H({})
    chat._api_chat(h)
    assert h.out == (400, {"ok": False, "error": "缺少 session_id"})


def test_api_chat_busy_returns_409(monkeypatch):
    monkeypatch.setattr(chat, "claim_active", lambda *a, **k: False)
    h = _H({"session_id": "s1"})
    chat._api_chat(h)
    code, obj = h.out
    assert code == 409 and obj["error"] == "busy"


def test_api_chat_early_reject_releases_lease(monkeypatch):
    released = []
    monkeypatch.setattr(chat, "claim_active", lambda *a, **k: True)
    monkeypatch.setattr(chat, "release_active", lambda sid, ev: released.append(sid))

    def fake_parse(body, sid, reject, question_policy="ask"):
        reject({"ok": False, "error": "会话不存在"}, 404)
        return None

    monkeypatch.setattr(chat, "parse_chat_request", fake_parse)
    monkeypatch.setattr(chat, "load_config", lambda: {})
    h = _H({"session_id": "s1"})
    chat._api_chat(h)
    assert released == ["s1"]
    assert h.out[0] == 404


def test_continue_basis_reads_last_assistant():
    s = {"messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "sources": [{"url": "u"}], "tools": [1, 2]},
    ]}
    assert chat._continue_basis(s, True) == ([{"url": "u"}], "a", 2, "")


def test_continue_basis_requires_assistant_message():
    s = {"messages": [{"role": "user", "content": "q"}]}
    _, _, _, err = chat._continue_basis(s, True)
    assert err == "no assistant message to continue"
    assert chat._continue_basis({}, False) == ([], None, None, "")
