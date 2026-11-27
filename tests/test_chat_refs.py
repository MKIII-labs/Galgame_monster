# -*- coding: utf-8 -*-
"""提问自动上下文：代码引用召回、公开引用裁剪与注入格式。"""
from app import context
from app.api import chat


def test_refs_public_dedupes_and_drops_text():
    refs = [
        {"type": "code", "path": "a.py", "start": 3, "end": 9, "symbol": "f",
         "auto": True, "text": "secret"},
        {"path": "a.py", "start": 3, "end": 9, "text": "dup"},
        {"type": "doc", "path": "b.md", "page": 2, "text": "doc"},
    ]
    out = chat._refs_public(refs)
    assert [x["path"] for x in out] == ["a.py", "b.md"]
    assert out[0]["start"] == 3 and out[0]["auto"] is True
    assert out[0]["type"] == "code"       # 回传前端时要保留类型
    assert out[1]["page"] == 2 and out[1]["type"] == "doc"
    assert all("text" not in x for x in out)


def test_last_user_refs_uses_most_recent_question():
    s = {"messages": [{"role": "user", "refs": [{"path": "old.py"}]},
                      {"role": "assistant", "content": "a"},
                      {"role": "user", "refs": [{"path": "new.py"}]}]}
    assert chat._last_user_refs(s)[0]["path"] == "new.py"


def test_auto_code_refs_disabled(monkeypatch):
    monkeypatch.setattr(chat, "load_config", lambda: {"chat_auto_context": False})
    assert chat._auto_code_refs({"sandbox": "x"}, "这个函数怎么用", []) == []


def test_auto_code_refs_builds_refs(monkeypatch):
    monkeypatch.setattr(chat, "load_config", lambda: {"chat_auto_context": True})
    from app import codeindex
    monkeypatch.setattr(codeindex, "status", lambda sb: {"state": "ready"})
    monkeypatch.setattr(codeindex, "search_items", lambda sb, q, limit=8: {"items": [
        {"path": "a.py", "start": 1, "end": 5, "symbol": "run",
         "score": 0.9, "keyword": False, "snippet": "def run():\n    pass"},
        {"path": "low.py", "start": 1, "end": 2, "symbol": "",
         "score": 0.05, "keyword": False, "snippet": "x"},
        {"path": "b.py", "start": 8, "end": 9, "symbol": "",
         "score": 0.1, "keyword": True, "snippet": "kw"},
    ]})
    refs = chat._auto_code_refs({"sandbox": "x"}, "这个函数怎么用", [])
    assert [r["path"] for r in refs] == ["a.py", "b.py"]
    assert refs[0]["start"] == 1 and refs[0]["auto"] is True
    assert "【引用代码" not in refs[0]["text"]   # 注入头由 context.py 统一加


def test_auto_code_refs_skips_explicit_path(monkeypatch):
    monkeypatch.setattr(chat, "load_config", lambda: {"chat_auto_context": True})
    from app import codeindex
    monkeypatch.setattr(codeindex, "status", lambda sb: {"state": "ready"})
    monkeypatch.setattr(codeindex, "search_items", lambda sb, q, limit=8: {"items": [
        {"path": "a.py", "start": 1, "end": 5, "symbol": "", "score": 0.9,
         "keyword": False, "snippet": "x"},
    ]})
    refs = chat._auto_code_refs({"sandbox": "x"}, "这个函数怎么用",
                                [{"path": "a.py"}])
    assert refs == []


def test_auto_code_refs_triggers_background_index(monkeypatch):
    monkeypatch.setattr(chat, "load_config", lambda: {"chat_auto_context": True})
    from app import codeindex
    calls = []
    monkeypatch.setattr(codeindex, "status", lambda sb: {"state": "idle"})
    monkeypatch.setattr(codeindex, "reindex",
                        lambda sb, background=True: calls.append(sb))
    assert chat._auto_code_refs({"sandbox": "x"}, "这个函数怎么用", []) == []
    assert calls == ["x"]


def test_context_injects_auto_code_ref():
    text = context._msg_text({
        "role": "user", "content": "怎么用",
        "refs": [{"path": "a.py", "start": 3, "end": 7, "symbol": "run",
                  "text": "def run():\n    pass"}],
    })
    assert "【引用代码：a.py:3-7 · run】" in text
    assert "def run():" in text


def test_context_injects_stored_quotes_with_label():
    """「添加到对话」的引用随消息落库后，后续追问 / 重新发言仍能进上下文。"""
    text = context._msg_text({
        "role": "user", "content": "这句话什么意思",
        "quotes": [{"text": "被引用的原文", "label": "笔记A"}],
    })
    assert "【用户引用的原文】" in text
    assert "[来源：笔记A]" in text
    assert "被引用的原文" in text


def test_context_injects_quote_only_message():
    text = context._msg_text({
        "role": "user", "content": "",
        "quotes": [{"text": "只有引用", "label": ""}],
    })
    assert text.startswith("【用户引用的原文】")
    assert "只有引用" in text


def test_normalize_quotes_skips_empty_and_clips():
    qs = context.normalize_quotes([
        {"text": "  ", "label": "空"},
        {"text": "x" * (context.QUOTE_MAX_ONE + 10), "label": "长"},
    ])
    assert len(qs) == 1
    assert qs[0]["text"].endswith("…（已截断）")
    assert len(qs[0]["text"]) == context.QUOTE_MAX_ONE + len("…（已截断）")
