# -*- coding: utf-8 -*-
"""省费模式（token_profile）：引用句柄、估算一致性、规则文件缓存。"""

import time

from app import agent, context


def _session():
    refs = [{"path": "src/a.py", "start": 1, "end": 20, "symbol": "foo",
             "text": "X" * 400},
            {"path": "docs/b.md", "text": "Y" * 400}]
    return {"messages": [
        {"role": "user", "content": "看这个", "refs": refs},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "再看这个", "refs": refs},
    ]}


def _cfg(profile):
    return {"token_profile": profile}


def test_history_refs_become_handles(monkeypatch):
    monkeypatch.setattr(context, "load_config", lambda: _cfg("balanced"))
    msgs = context.build_context_messages(_session(), "SYS")
    # 历史里的引用：只留定位信息，正文不进上下文
    assert "正文未内联" in msgs[1]["content"]
    assert "X" * 100 not in msgs[1]["content"]
    assert "src/a.py:1-20" in msgs[1]["content"]
    # 最后一条用户消息仍然内联，模型当轮直接看得到
    assert "Y" * 100 in msgs[3]["content"]


def test_quality_mode_keeps_inline_refs(monkeypatch):
    monkeypatch.setattr(context, "load_config", lambda: _cfg("quality"))
    msgs = context.build_context_messages(_session(), "SYS")
    assert "X" * 100 in msgs[1]["content"]
    assert "Y" * 100 in msgs[3]["content"]
    assert "正文未内联" not in msgs[1]["content"]


def test_estimates_drop_in_balanced_mode(monkeypatch):
    monkeypatch.setattr(context, "load_config", lambda: _cfg("quality"))
    full = context.session_context(_session(), "SYS")["used"]
    monkeypatch.setattr(context, "load_config", lambda: _cfg("balanced"))
    lean = context.session_context(_session(), "SYS")["used"]
    assert lean < full, (lean, full)
    # 句柄只替换正文，定位信息仍在，所以不会掉到一半以下
    assert lean > full * 0.5, (lean, full)


def test_message_without_refs_is_unchanged(monkeypatch):
    monkeypatch.setattr(context, "load_config", lambda: _cfg("balanced"))
    only_text = {"messages": [{"role": "user", "content": "纯文本"}, {"role": "assistant", "content": "好"}]}
    msgs = context.build_context_messages(only_text, "SYS")
    assert msgs[1]["content"] == "纯文本"


def test_project_rules_cache_invalidates_on_change(tmp_path):
    f = tmp_path / "AGENTS.md"
    f.write_text("v1 规则", encoding="utf-8")
    assert "v1 规则" in agent.project_rules(str(tmp_path))
    # 没改动：重复调用结果一致（缓存命中）
    assert agent.project_rules(str(tmp_path)) == agent.project_rules(str(tmp_path))
    time.sleep(0.01)
    f.write_text("v2 规则", encoding="utf-8")
    assert "v2 规则" in agent.project_rules(str(tmp_path))


def test_project_rules_cache_drops_deleted_file(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("会被删掉", encoding="utf-8")
    assert "会被删掉" in agent.project_rules(str(tmp_path))
    f.unlink()
    assert agent.project_rules(str(tmp_path)) == ""
