# -*- coding: utf-8 -*-
"""按需加载（lazy context）：自动召回预算、重复读去重、落盘关键行、文件大纲。"""

import os

import pytest

from app import agent, config, context, repomap, spill


# ---------------------------------------------------------------- 落盘关键行
def test_key_lines_picks_errors_and_dedupes():
    text = "\n".join([
        "step 1 ok", "step 2 ok",
        "ERROR: something failed", "step 3 ok",
        "ERROR: something failed",
        "Traceback (most recent call last):",
        "warning: deprecated",
    ])
    rows = spill.key_lines(text, limit=10)
    assert rows[0] == "ERROR: something failed"
    assert len(rows) == 3                      # 去重后剩 3 条
    assert any("Traceback" in r for r in rows)
    assert any("warning" in r for r in rows)


def test_key_lines_caps_and_handles_empty():
    assert spill.key_lines("", limit=5) == []
    assert spill.key_lines(None, limit=5) == []
    rows = spill.key_lines("\n".join("error %d" % i for i in range(50)), limit=5)
    assert len(rows) == 5


# ---------------------------------------------------------------- 文件大纲
def test_outline_python_file(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("def a(x):\n    return x\n\nclass B:\n    def c(self):\n        pass\n",
                 encoding="utf-8")
    out = repomap.outline(str(p))
    assert "def" in out and "a(x)" in out
    assert "class" in out and "B" in out


def test_outline_non_code_returns_empty(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello\n", encoding="utf-8")
    assert repomap.outline(str(p)) == ""


# ---------------------------------------------------------------- 重复读去重
def _caps(turn="t1", sid="s1"):
    return {"sid": sid, "turn_key": turn, "read_dedupe": True}


def test_repeat_read_is_shortened(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("print(1)\nprint(2)\n", encoding="utf-8")
    first, _u, _w = agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"},
                                   caps=_caps())
    assert "print(1)" in first
    second, _u, _w = agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"},
                                    caps=_caps())
    assert "未变化" in second and "print(1)" not in second


def test_repeat_read_with_range_bypasses(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("print(1)\nprint(2)\n", encoding="utf-8")
    agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"}, caps=_caps())
    third, _u, _w = agent.run_tool(str(tmp_path), "read_file",
                                   {"path": "a.py", "start": 1}, caps=_caps())
    assert "print(1)" in third


def test_repeat_read_other_turn_not_shortened(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("print(1)\n", encoding="utf-8")
    agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"}, caps=_caps("t1"))
    other, _u, _w = agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"},
                                   caps=_caps("t2"))
    assert "print(1)" in other            # 换回合后正文必须重新给


def test_repeat_read_after_change_not_shortened(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("print(1)\n", encoding="utf-8")
    agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"}, caps=_caps())
    p.write_text("print(2)\nprint(3)\n", encoding="utf-8")
    again, _u, _w = agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"},
                                   caps=_caps())
    assert "print(2)" in again


def test_repeat_read_can_be_disabled(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("print(1)\n", encoding="utf-8")
    caps = {"sid": "s1", "turn_key": "t1", "read_dedupe": False}
    agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"}, caps=caps)
    again, _u, _w = agent.run_tool(str(tmp_path), "read_file", {"path": "a.py"},
                                   caps=caps)
    assert "print(1)" in again


# ---------------------------------------------------------------- 自动召回预算
def test_auto_context_budget_inlines_only_top_items(monkeypatch, tmp_path):
    from app.api import chat as chat_mod
    from app import codeindex

    monkeypatch.setattr(codeindex, "status", lambda root: {"state": "ready"})
    items = [{"path": "f%d.py" % i, "start": 1, "end": 10,
              "score": 0.9 - i * 0.1, "snippet": "x" * 3000}
             for i in range(4)]
    monkeypatch.setattr(codeindex, "search_items",
                        lambda root, q, limit=8: {"items": items})
    monkeypatch.setattr(chat_mod, "load_config",
                        lambda: {"chat_auto_context": True,
                                 "auto_context_budget_chars": 8000,
                                 "auto_context_inline_max": 2,
                                 "auto_context_items": 5})
    refs = chat_mod._auto_code_refs({"sandbox": str(tmp_path)}, "请帮我找一下这个逻辑")
    assert len(refs) == 4
    assert sum(1 for r in refs if not r.get("slim")) == 2   # 只有前两段内联
    assert all(r.get("slim") for r in refs[2:])


def test_auto_context_respects_item_limit(monkeypatch, tmp_path):
    from app.api import chat as chat_mod
    from app import codeindex

    monkeypatch.setattr(codeindex, "status", lambda root: {"state": "ready"})
    items = [{"path": "f%d.py" % i, "start": 1, "end": 2, "score": 0.5,
              "snippet": "y" * 10} for i in range(9)]
    monkeypatch.setattr(codeindex, "search_items",
                        lambda root, q, limit=8: {"items": items})
    monkeypatch.setattr(chat_mod, "load_config",
                        lambda: {"chat_auto_context": True,
                                 "auto_context_items": 3})
    refs = chat_mod._auto_code_refs({"sandbox": str(tmp_path)}, "请帮我找一下这个逻辑")
    assert len(refs) == 3


# ---------------------------------------------------------------- 渲染与配置
def test_msg_text_renders_slim_ref_without_body():
    m = {"role": "user", "content": "看这个",
         "refs": [{"path": "a.py", "start": 3, "end": 8, "slim": True,
                   "text": "SECRET_BODY"}]}
    out = context._msg_text(m)
    assert "a.py:3-8" in out
    assert "SECRET_BODY" not in out
    assert "正文未内联" in out


def test_msg_text_keeps_body_when_not_slim():
    m = {"role": "user", "content": "看这个",
         "refs": [{"path": "a.py", "start": 3, "end": 8, "text": "BODY_123"}]}
    assert "BODY_123" in context._msg_text(m)


def test_new_config_defaults():
    d = config.DEFAULT_CONFIG
    assert d["tool_spill_max"] == 12000
    assert d["agent_read_dedupe"] is True
    assert d["read_outline_mode"] is False
    assert d["auto_context_inline_max"] == 2
    assert d["repo_summary_inject"] is False
