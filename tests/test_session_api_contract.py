# -*- coding: utf-8 -*-
"""新版界面（P2）用到的会话接口契约：payload 形状不能变。

只验证「请求 → 返回」的结构与副作用，不读写真实 data/ 目录：
load_sessions / save_sessions / set_current / 附件与存档清理全部替换成内存实现。
"""
import pytest

from app.api import sessions as api
from app.sessions import slim_sessions


def test_slim_sessions_drops_attachment_text():
    """前端 payload 不带附件正文：只保留 name/size/chars 等展示字段。"""
    data = {"current": "s1", "sessions": [{
        "id": "s1", "title": "t", "messages": [{
            "role": "user", "content": "看这份讲义",
            "docs": [{"name": "a.pdf", "file": "f1.pdf", "chars": 1234,
                      "text": "很长的正文" * 100}],
            "doc_text": "旧的兼容字段正文",
            "refs": [{"path": "x.py", "text": "引用正文"}],
        }],
    }]}
    msg = slim_sessions(data)["sessions"][0]["messages"][0]
    assert "doc_text" not in msg
    assert msg["docs"][0]["name"] == "a.pdf"
    assert msg["docs"][0]["chars"] == 1234
    assert "text" not in msg["docs"][0]
    assert msg["refs"][0]["path"] == "x.py"      # 引用正文不在本次裁剪范围内
    # 原始数据不能被改写（服务端后续还要用它构建上下文）
    assert data["sessions"][0]["messages"][0]["docs"][0]["text"]
    assert data["sessions"][0]["messages"][0]["doc_text"]


class FakeHandler:
    def __init__(self, body=None):
        self.body = body or {}
        self.calls = []

    def _body(self):
        return self.body

    def _json(self, obj, code=200):
        self.calls.append((code, obj))
        return obj

    def _ok_sessions(self, data):
        # 与 app/httpd.py Handler._ok_sessions 保持同样的外层结构
        self._json({"ok": True, "sessions": slim_sessions(data), "context": None})

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture()
def store(monkeypatch):
    data = {
        "current": "s_a",
        "sessions": [
            {"id": "s_a", "title": "会话 A", "updated_at": 200, "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "在的"},
            ]},
            {"id": "s_b", "title": "会话 B", "updated_at": 100, "messages": []},
        ],
    }
    saved = []
    monkeypatch.setattr(api, "load_sessions", lambda: data)
    monkeypatch.setattr(api, "save_sessions", lambda d: saved.append(d))
    monkeypatch.setattr(api, "set_current", lambda sid: data.update({"current": sid}))
    monkeypatch.setattr(api, "new_id", lambda: "s_new")
    monkeypatch.setattr(api, "delete_session_uploads", lambda sid: None)
    monkeypatch.setattr(api.branch_lib, "drop_legacy_saves", lambda sid: None)
    # 新建会话会跑 SessionStart 钩子：测试里关掉，避免碰真实 data/
    import app.hooks as hooks
    monkeypatch.setattr(hooks, "session_start", lambda *a, **k: "")
    return data, saved


def test_slim_payload_shape():
    """新版侧栏依赖 current + sessions[].{id,title,updated_at,pinned,msg_total}。"""
    data = {"current": "s_a", "sessions": [
        {"id": "s_a", "title": "A", "updated_at": 1, "messages": [{"role": "user", "content": "x"}]},
        {"id": "s_b", "title": "B", "updated_at": 2, "messages": [{"role": "user", "content": "y"}], "pinned": True},
    ]}
    out = slim_sessions(data)
    assert out["current"] == "s_a"
    rows = out["sessions"]
    assert [r["id"] for r in rows] == ["s_b", "s_a"]          # 置顶优先
    for r in rows:
        for key in ("id", "title", "updated_at", "pinned", "msg_total"):
            assert key in r
    # 非当前会话不携带消息（预览走 /api/session/messages）
    assert rows[0]["messages"] is None
    assert rows[1]["messages"]


def test_new_session_becomes_current(store):
    data, saved = store
    h = FakeHandler({})
    api._api_session_new(h)
    code, obj = h.last
    assert code == 200 and obj["ok"] is True
    assert obj["sessions"]["current"] == "s_new"
    assert saved and saved[-1]["sessions"][0]["id"] == "s_new"


def test_rename_and_pin(store):
    data, _ = store
    h = FakeHandler({"id": "s_b", "title": "改名后"})
    api._api_session_rename(h)
    assert data["sessions"][1]["title"] == "改名后"

    h2 = FakeHandler({"id": "s_b", "pinned": True})
    api._api_session_pin(h2)
    assert data["sessions"][1]["pinned"] is True
    assert h2.last[1]["sessions"]["sessions"][0]["id"] == "s_b"   # 置顶后排到最前


def test_select_changes_current(store):
    data, _ = store
    h = FakeHandler({"id": "s_b"})
    api._api_session_select(h)
    assert data["current"] == "s_b"
    assert h.last[1]["sessions"]["current"] == "s_b"


def test_delete_refuses_last_session(store):
    data, _ = store
    h = FakeHandler({"id": "s_b"})
    api._api_session_delete(h)
    assert [s["id"] for s in data["sessions"]] == ["s_a"]
    assert h.last[1]["sessions"]["current"] == "s_a"

    h2 = FakeHandler({"id": "s_a"})
    api._api_session_delete(h2)
    code, obj = h2.last
    # 只剩一个会话时拒绝删除：ok=False，且不能把整库原样塞回给前端
    assert obj["ok"] is False
    assert len(data["sessions"]) == 1


def test_messages_pagination(store):
    data, _ = store
    data["sessions"][0]["messages"] = [{"role": "user", "content": str(i)} for i in range(30)]
    h = FakeHandler({"session_id": "s_a", "limit": 10})
    api._api_session_messages(h)
    obj = h.last[1]
    assert obj["total"] == 30 and obj["before"] == 30 and obj["from"] == 20
    assert len(obj["messages"]) == 10
    assert obj["messages"][-1]["content"] == "29"

    h2 = FakeHandler({"session_id": "s_a", "before": 20, "limit": 5})
    api._api_session_messages(h2)
    obj2 = h2.last[1]
    assert obj2["from"] == 15 and obj2["before"] == 20


def test_messages_missing_session(store):
    h = FakeHandler({"session_id": "nope"})
    api._api_session_messages(h)
    code, obj = h.last
    assert code == 404 and obj["ok"] is False
