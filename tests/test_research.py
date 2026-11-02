# -*- coding: utf-8 -*-
"""研究台账 + 检索整理 + 研究闸门的回归测试（全部离线）。

覆盖点：
  * 来源编号跨调用连续、URL 去重、空结果不算检索
  * 缺口 / 结论的登记与关闭、跨进程重读
  * 引用编号核验
  * 正文摘要只保留相关段落
  * 研究闸门的放行 / 拦截 / 防死锁
  * 子代理调研模式的来源核验与验收条件
"""
import json
import os

from app import research
from app.search import _excerpt, build_search_context


def _hit(url, title="t"):
    return {"title": title, "url": url, "site": "example.com", "snippet": "snip"}


# ---------------------------------------------------------------- 任务识别

def test_is_research_task():
    assert research.is_research_task("尽可能还原 mc")
    assert research.is_research_task("复刻一个贪吃蛇")
    assert research.is_research_task("从零实现一个 KV 存储")
    assert research.is_research_task("照着这个网站做个 clone")
    assert not research.is_research_task("帮我改一下这个 bug")
    assert not research.is_research_task("")


# ---------------------------------------------------------------- 来源台账

def test_add_sources_dedupes_and_numbers():
    led = research.Ledger("s_src")
    out = led.add_sources([_hit("https://a.com/1"), _hit("https://b.com/2")])
    assert [n for n, _ in out] == [1, 2]
    assert led.searches == 1
    # 重复 URL 复用旧编号，不会从 1 重排
    out2 = led.add_sources([_hit("https://b.com/2"), _hit("https://c.com/3")])
    assert [n for n, _ in out2] == [2, 3]
    assert led.searches == 2
    assert [s["n"] for s in led.sources] == [1, 2, 3]


def test_empty_hits_do_not_count_as_search():
    led = research.Ledger("s_empty")
    assert led.add_sources([]) == []
    assert led.searches == 0


def test_gaps_and_claims():
    led = research.Ledger("s_gap")
    assert led.add_gaps(["合成表", "红石时序", "合成表"]) == ["g1", "g2"]
    assert led.stats()["gaps"] == 2
    assert led.close_gaps(["g1"]) == ["g1"]
    assert led.stats()["gaps"] == 1
    assert led.add_claims([{"text": "刻是 20Hz", "sources": [1, 2]}]) == ["c1"]
    assert led.claims[0]["sources"] == [1, 2]


def test_bad_citations():
    led = research.Ledger("s_cite")
    led.add_sources([_hit("https://a.com/1")])
    assert led.bad_citations("依据 [1]，另见 [2]") == [2]
    assert led.bad_citations("没有引用") == []
    empty = research.Ledger("s_cite_empty")
    assert empty.bad_citations("[5]") == []      # 没有来源时不做判定


def test_context_block_contains_gaps_and_sources():
    led = research.Ledger("s_ctx")
    led.add_sources([_hit("https://a.com/1", "方块表")])
    led.add_gaps(["红石时序"])
    block = led.context_block()
    assert "研究台账" in block
    assert "红石时序" in block
    assert "https://a.com/1" in block


def test_context_block_includes_snippet():
    """台账只给标题 + URL 时信息密度太低，模型看不出「这条来源讲了什么」。"""
    led = research.Ledger("s_snip")
    led.add_sources([{"title": "合成表", "url": "https://a.com/craft",
                      "site": "wiki", "snippet": "工作台由四块木板组成"}])
    block = led.context_block()
    assert "摘要：" in block
    assert "工作台由四块木板组成" in block


def test_native_web_search_schema_mentions_multi_round():
    """原生通道只看 schema 描述：这里必须写明复刻类任务要分轮补搜。"""
    from app import agent
    schemas = agent.native_tool_schemas(agent_on=True, search_ready=True)
    ws = next(t for t in schemas if t["function"]["name"] == "web")
    assert "分轮" in ws["function"]["description"]


def test_research_prompt_short_mentions_ledger_loop():
    from app import agent
    text = agent._RESEARCH_PROMPT_SHORT
    assert "gap_done" in text
    assert "补" in text


def test_search_multi_respects_merged_max(monkeypatch):
    """研究档放宽合并条数：上限由调用方传入，不再写死 MAX_MERGED。"""
    from app import search

    def _fake(q, *_a, **_kw):
        return [{"title": "%s-%d" % (q, i), "url": "https://%s/%d" % (q, i)}
                for i in range(5)]

    monkeypatch.setattr(search, "search_web", _fake)
    hits = search.search_multi(["a", "b", "c"], "x", "", "k", 5, merged_max=4)
    assert len(hits) == 4


def test_ledger_persists(tmp_path):
    led = research.Ledger("s_persist")
    led.add_sources([_hit("https://a.com/1")])
    led.add_gaps(["缺口 A"])
    path = led._path()
    assert os.path.isfile(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["sources"][0]["url"] == "https://a.com/1"
    again = research.Ledger("s_persist")
    assert again.stats()["sources"] == 1
    assert again.stats()["gaps"] == 1


def test_note_actions():
    ok, text = research.note({"action": "gap_add", "gaps": ["方块 id 表"]}, "s_note")
    assert ok and "g1" in text
    ok, _ = research.note({"action": "gap_done", "ids": ["g1"]}, "s_note")
    assert ok
    ok, text = research.note({"action": "status"}, "s_note")
    assert ok and "研究台账" in text
    ok, _ = research.note({"action": "nope"}, "s_note")
    assert not ok


def test_get_reuses_instance():
    a = research.get("s_same")
    b = research.get("s_same")
    assert a is b


# ---------------------------------------------------------------- 检索整理

def test_build_search_context_uses_global_numbers():
    ctx = build_search_context("q", [{"title": "T", "url": "u", "site": "", "n": 7}])
    assert "[7] T" in ctx
    ctx2 = build_search_context("q", [{"title": "T", "url": "u", "site": ""}], start=3)
    assert "[3] T" in ctx2


def test_excerpt_prefers_relevant_paragraphs():
    text = ("导航项\n版权声明\n" + "无关内容。\n" * 40
            + "红石中继器延迟是 2 刻。\n" + "页脚信息。\n" * 40)
    out = _excerpt(text, "红石 中继器 延迟", limit=60)
    assert "红石中继器" in out
    assert len(out) <= 80


# ---------------------------------------------------------------- 研究闸门

def test_research_gate_blocks_until_enough_searches():
    from app.api.chat import _research_gate_action
    state = {"task": True, "granted": False, "blocked": 0}
    assert _research_gate_action(state, "write_file", {"searches": 0}, 2) == "block"
    assert _research_gate_action(state, "write_file", {"searches": 1}, 2) == "block"
    assert _research_gate_action(state, "write_file", {"searches": 2}, 2) == "allow"
    assert state["granted"] is True


def test_research_gate_releases_after_two_blocks():
    from app.api.chat import _research_gate_action
    state = {"task": True, "granted": False, "blocked": 0}
    assert _research_gate_action(state, "edit_file", {}, 2) == "block"
    assert _research_gate_action(state, "edit_file", {}, 2) == "block"
    assert _research_gate_action(state, "edit_file", {}, 2) == "allow_after_block"
    assert state["granted"] is True


def test_research_gate_ignores_read_tools_and_disabled():
    from app.api.chat import _research_gate_action
    no_task = {"task": False, "granted": False, "blocked": 0}
    assert _research_gate_action(no_task, "write_file", {}, 2) == "allow"
    state = {"task": True, "granted": False, "blocked": 0}
    assert _research_gate_action(state, "read_file", {}, 2) == "allow"
    assert _research_gate_action(state, "write_file", {}, 0) == "allow"


def test_clear_ledger_resets_searches():
    """新任务不能沿用旧任务的检索次数 / 来源。"""
    led = research.Ledger("s_clear")
    led.add_sources([_hit("https://a.com/1")])
    assert research.Ledger("s_clear").stats()["searches"] == 1

    research.clear_ledger("s_clear")
    st = research.Ledger("s_clear").stats()
    assert st == {"searches": 0, "sources": 0, "gaps": 0, "claims": 0}


def test_reset_task_state_clears_ledger_and_plan():
    from app.api.chat import _reset_task_state
    led = research.Ledger("s_reset")
    led.add_sources([_hit("https://a.com/1")])
    s = {"id": "s_reset", "plan": {"steps": [{"text": "旧计划", "status": "doing"}]}}
    _reset_task_state(s)
    assert "plan" not in s
    assert research.Ledger("s_reset").stats()["searches"] == 0


# ---------------------------------------------------------------- 子代理调研

def test_verify_sources_flags_invented_links():
    from app.explore import _verify_sources
    res = _verify_sources("见 https://a.com/x 与 https://fake.com/y",
                          {"https://a.com/x"})
    assert res["missing"] == ["https://fake.com/y"]
    assert res["score"] == 0.5


def test_explore_acceptance_sources_and_cross():
    from app.explore import _check_acceptance
    results = [{"ok": True, "text": "见 https://a.com/x 与 https://b.com/y"}]
    assert _check_acceptance(["sources>=2", "cross>=2"], results)["passed"]
    assert not _check_acceptance(["cross>=3"], results)["passed"]


def test_explore_research_mode_assigns_searcher():
    from app.explore import normalize_args
    norm, err = normalize_args({"goal": "查方块表", "questions": ["方块 id 有哪些"],
                                "research": True}, 5)
    assert not err
    assert norm["research"] is True
    assert norm["questions"][0]["role"] == "searcher"
    # 调研与写入互斥：同时给 allow_write 也不会变成 worker
    norm2, _ = normalize_args({"goal": "g", "questions": ["q"],
                               "research": True, "allow_write": True}, 5)
    assert norm2["questions"][0]["role"] == "searcher"
    assert norm2["allow_write"] is False
