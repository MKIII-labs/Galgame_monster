# -*- coding: utf-8 -*-
"""多会话并行：落盘只替换本会话，不能互相覆盖"""
import threading

from app import sessions as sess
from app.api import chat as chat_api


def _seed(monkeypatch, tmp_path):
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(sess, "SESSION_FILE", str(path))
    sess.save_sessions({"current": "a", "sessions": [
        {"id": "a", "title": "A", "messages": [], "updated_at": 0},
        {"id": "b", "title": "B", "messages": [], "updated_at": 0},
    ]})
    return path


def test_persist_replaces_only_one_session(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    base = sess.load_sessions()
    sa = sess.find_session(base, "a")
    sb = sess.find_session(base, "b")
    sa["messages"].append({"role": "user", "content": "A1"})
    sb["messages"].append({"role": "assistant", "content": "B1"})

    chat_api._persist_session(sa)
    chat_api._persist_session(sb)

    final = sess.load_sessions()
    assert [m["content"] for m in sess.find_session(final, "a")["messages"]] == ["A1"]
    assert [m["content"] for m in sess.find_session(final, "b")["messages"]] == ["B1"]
    assert final["current"] == "a"          # current 不因落盘而改变


def test_persist_concurrent_does_not_lose(monkeypatch, tmp_path):
    """模拟两个会话同时生成：各自持有开头读到的全量快照，同时落盘。"""
    _seed(monkeypatch, tmp_path)
    base = sess.load_sessions()
    sa = sess.find_session(base, "a")
    sb = sess.find_session(base, "b")
    sa["messages"].append({"role": "user", "content": "A1"})
    sb["messages"].append({"role": "user", "content": "B1"})

    barrier = threading.Barrier(2)

    def work(s):
        barrier.wait()
        chat_api._persist_session(s)

    ts = [threading.Thread(target=work, args=(sa,)), threading.Thread(target=work, args=(sb,))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)

    final = sess.load_sessions()
    assert len(sess.find_session(final, "a")["messages"]) == 1
    assert len(sess.find_session(final, "b")["messages"]) == 1


def test_persist_ignores_bad_input():
    assert chat_api._persist_session(None) is None
    assert chat_api._persist_session({}) is None
