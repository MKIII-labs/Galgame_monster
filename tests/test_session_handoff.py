# -*- coding: utf-8 -*-
"""new_context 对齐：新会话 + 交接摘要。"""
import threading

from app import sessions as S
from app.api import sessions as api_s


class FakeHandler:
    def __init__(self, body=None):
        self._incoming = body or {}
        self.calls = []

    def _json(self, obj, code=200):
        self.calls.append((code, obj))
        return obj

    def _body(self):
        return self._incoming

    def _ok_sessions(self, data):
        self._json({"ok": True, "sessions": S.slim_sessions(data),
                    "context": {}})

    def last(self):
        return self.calls[-1]


def _seed():
    s = {"id": "src1", "title": "长任务", "updated_at": 1.0,
         "agent_enabled": True, "sandbox": "D:/proj", "approval_mode": "ai",
         "task_ledger": {"goal": "把导入器写完",
                         "steps": [{"text": "读现有导入器", "status": "done"},
                                   {"text": "补 CSV 支持", "status": "pending"}],
                         "acceptance": ["pytest 全绿"]},
         "messages": [{"role": "user", "content": "继续做导入器"},
                      {"role": "assistant", "content": "已读完现有实现，下一步补 CSV"}]}
    S.save_sessions({"current": "src1", "sessions": [s]})
    return s


def test_handoff_text_contains_key_context():
    s = _seed()
    text = api_s._handoff_text(s, note="用 Python 3.13")
    assert "把导入器写完" in text
    assert "1/2 步完成" in text and "补 CSV 支持" in text
    assert "继续做导入器" in text
    assert "pytest 全绿" in text and "Python 3.13" in text


def test_handoff_creates_new_session_and_switches():
    _seed()
    h = FakeHandler({"session_id": "src1"})
    api_s._api_session_handoff(h)
    code, obj = h.last()
    assert code == 200 and obj["ok"] is True
    payload = obj["sessions"]
    rows = payload["sessions"] if isinstance(payload, dict) else payload
    assert len(rows) == 2
    new = rows[0]
    if isinstance(payload, dict):
        assert payload["current"] == new["id"]
    assert new["id"] != "src1" and new["title"].endswith("· 续")
    assert new["sandbox"] == "D:/proj" and new["agent_enabled"] is True
    content = new["messages"][0]["content"]
    assert "【上一轮交接】" in content and "补 CSV 支持" in content


def test_handoff_rejects_unknown_and_streaming():
    _seed()
    h = FakeHandler({"session_id": "nope"})
    api_s._api_session_handoff(h)
    code, obj = h.last()
    assert code == 404 and obj["ok"] is False

    from app.store import ACTIVE_STREAMS
    ACTIVE_STREAMS["src1"] = threading.Event()
    try:
        h2 = FakeHandler({"session_id": "src1"})
        api_s._api_session_handoff(h2)
        code2, obj2 = h2.last()
        assert code2 == 409 and obj2["ok"] is False
    finally:
        ACTIVE_STREAMS.pop("src1", None)
