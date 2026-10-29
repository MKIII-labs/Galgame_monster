# -*- coding: utf-8 -*-
"""首轮会话标题：清洗模型输出、只在默认标题时生成、手动改名后不再覆盖。"""
import json

import pytest

from app import sessions as store
from app.api import chat


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SESSION_FILE", str(tmp_path / "sessions.json"))
    store.reset_merge_flag()
    yield tmp_path
    store.reset_merge_flag()


def _write_sessions(path, s):
    path.write_text(json.dumps({"current": s["id"], "sessions": [s]},
                               ensure_ascii=False), encoding="utf-8")


def _session(title="新对话", source=None):
    s = {
        "id": "s1", "title": title, "updated_at": 1.0,
        "messages": [
            {"role": "user", "content": "帮我看看这个登录接口为什么一直 401"},
            {"role": "assistant", "content": "先检查请求头里的 Authorization 是否带上了 token。"},
        ],
    }
    if source:
        s["title_source"] = source
    return s


def test_clean_title_removes_quotes_and_prefix():
    assert chat._clean_session_title('“重构登录流程。”') == "重构登录流程"
    assert chat._clean_session_title("标题：排查 401。") == "排查 401"
    assert chat._clean_session_title("```\n给笔记加双链\n```") == "给笔记加双链"


def test_clean_title_limits_length():
    long = "这是一个特别特别特别特别特别特别长的标题"
    assert len(chat._clean_session_title(long)) <= chat.TITLE_MAX_CHARS


def test_title_excerpt_strips_code_blocks():
    text = "看这段：\n```python\nprint(1)\n```\n然后再说结论"
    assert "print" not in chat._title_excerpt(text, 200)
    assert "结论" in chat._title_excerpt(text, 200)


def test_auto_title_generates_once(env, monkeypatch):
    _write_sessions(env / "sessions.json", _session())
    calls = {"n": 0}

    def fake_quick(api_key, api_url, api_model, messages, **kw):
        calls["n"] += 1
        assert kw.get("source") == "session_title"
        assert "登录接口" in messages[-1]["content"]
        return "排查登录 401"

    monkeypatch.setattr(chat, "_quick_chat", fake_quick)
    s = store.load_sessions()["sessions"][0]
    assert chat._auto_title_session(s, "k", "u", "m") == "排查登录 401"

    saved = store.load_sessions()["sessions"][0]
    assert saved["title"] == "排查登录 401"
    assert saved["title_source"] == "auto"

    # 已标记 auto：第二轮不会再调用模型
    assert chat._auto_title_session(saved, "k", "u", "m") == ""
    assert calls["n"] == 1


def test_auto_title_skips_manual_and_existing(env, monkeypatch):
    _write_sessions(env / "sessions.json", _session("我自己起的", "manual"))
    monkeypatch.setattr(chat, "_quick_chat",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应调用模型")))
    s = store.load_sessions()["sessions"][0]
    assert chat._auto_title_session(s, "k", "u", "m") == ""

    # 已经由旧逻辑起过名的会话也不再动
    _write_sessions(env / "sessions.json", _session("旧标题"))
    s = store.load_sessions()["sessions"][0]
    assert chat._auto_title_session(s, "k", "u", "m") == ""


def test_auto_title_keeps_default_on_model_failure(env, monkeypatch):
    _write_sessions(env / "sessions.json", _session())
    monkeypatch.setattr(chat, "_quick_chat",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("timeout")))
    s = store.load_sessions()["sessions"][0]
    assert chat._auto_title_session(s, "k", "u", "m") == ""
    assert store.load_sessions()["sessions"][0]["title"] == "新对话"
