# -*- coding: utf-8 -*-
"""提示词预算回归：体积直接决定首 token 延迟，涨回去要在这里被发现"""
import json

import pytest

from app import agent, mem_tool  # noqa: F401
from app import httpd  # noqa: F401  导入即注册能力（bili / calendar / stock / memory / observe）


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch):
    monkeypatch.setattr(mem_tool, "get_profile_kb", lambda: "")
    monkeypatch.setattr(mem_tool, "load_profile", lambda: {"auto_learn_enabled": False})


def _nodes(n=30):
    return [{"id": "n%d" % i, "title": "条目标题 %d" % i,
             "content": "这是一段比较长的正文，用来模拟真实记忆条目。" * 2}
            for i in range(n)]


def _sess(tmp_path, msgs=3):
    return {"agent_enabled": True, "sandbox": str(tmp_path), "ai_write": True,
            "memory": {"nodes": _nodes(), "edges": []},
            "messages": [{"role": "user" if i % 2 == 0 else "assistant",
                          "content": "第 %d 条" % i} for i in range(msgs)]}


def test_memory_prompt_stays_small(tmp_path):
    text = mem_tool.build_tool_prompt(_sess(tmp_path))
    assert len(text) < 5000, "记忆工具说明 %d 字，超出预算（每轮都要重发）" % len(text)
    assert "append_note" in text          # 关键用法不能为了省字丢掉
    assert "本会话记忆不受此限" in text


def test_agent_prompt_without_domains_is_compact(tmp_path):
    text = agent.build_agent_prompt(_sess(tmp_path), search_ready=True,
                                    native_tools=True, cap_domains=set())
    # 43.201：计划 v2 增加「里程碑 / verify / plan_patch」说明，阈值从 7000 调到 7400
    # 2026-10-29：读类工具新增 raw / 上下文 / 计数说明（减少「看文件写脚本」），7400 → 7600
    assert len(text) < 7600, "Agent 提示 %d 字" % len(text)
    assert "沙箱目录树" not in text            # 有历史 + 原生工具 → 不再每轮塞树
    assert "calendar.create" in text           # 提醒约定必须保留
    assert "bili.analyze" not in text          # 未命中 B 站域 → 相关长约定不注入


def test_agent_prompt_first_turn_keeps_tree(tmp_path):
    s = _sess(tmp_path, msgs=0)
    text = agent.build_agent_prompt(s, search_ready=True, native_tools=True, cap_domains=set())
    assert "沙箱目录树" in text


def test_schema_gating_by_domain():
    none_dom = agent.native_tool_schemas(agent_on=True, search_ready=True, cap_domains=set())
    names = [t["function"]["name"] for t in none_dom]
    assert "bili_analyze" not in names and "calendar_create" not in names
    bili = agent.native_tool_schemas(agent_on=True, search_ready=True, cap_domains={"bili"})
    bnames = [t["function"]["name"] for t in bili]
    assert "bili_analyze" in bnames and "calendar_create" not in bnames
    assert len(bili) > len(none_dom)


def test_schema_always_keeps_all():
    full = agent.native_tool_schemas(agent_on=True, search_ready=True, cap_domains=None)
    gated = agent.native_tool_schemas(agent_on=True, search_ready=True, cap_domains=set())
    assert len(full) > len(gated)
    assert len(json.dumps(full, ensure_ascii=False)) > 10000


def test_tool_schema_groups_hidden_by_default():
    """A档：低频工具组默认不下发，命中意图或 tool_search 解锁后才回来。"""
    full = agent.native_tool_schemas(agent_on=True, search_ready=True, cap_domains=None)
    gated = agent.native_tool_schemas(agent_on=True, search_ready=True,
                                      cap_domains=set(), allow_groups=set())
    names = lambda xs: {t["function"]["name"] for t in xs}
    assert len(gated) < len(full)
    assert "read_file" in names(gated)          # 核心工具始终在
    assert "render_check" not in names(gated)   # 低频组被收起
    assert "tool_search" in names(gated)        # 但给了按需解锁入口
    vis = agent.native_tool_schemas(agent_on=True, search_ready=True,
                                    cap_domains=set(), allow_groups={"visual"})
    assert "render_check" in names(vis)
    unl = agent.native_tool_schemas(agent_on=True, search_ready=True,
                                    cap_domains=set(), allow_groups=set(),
                                    unlocked={"asset"})
    assert "asset_scan" in names(unl)


def test_tool_search_unlocks_group():
    caps = {}
    text = agent._t_tool_search({"query": "素材"}, caps)
    assert "asset" in caps.get("tool_unlock", set())
    assert "asset_scan" in text
    hidden = agent.hidden_tools(set(), caps["tool_unlock"])
    assert hidden.isdisjoint(set(agent.TOOL_GROUPS["asset"]))


def test_cap_domains_for_keywords():
    from app.api import chat
    assert "bili" in chat._cap_domains_for("帮我看看这个 BV1xx411c7mD 视频")
    assert "calendar" in chat._cap_domains_for("明天早上提醒我交作业")
    assert chat._cap_domains_for("今天天气不错") == set()


def test_rank_items_can_skip_model():
    from app import knowledge
    items = [{"node": {"id": "n%d" % i, "title": "条目 %d" % i, "content": "量子 比特",
                       "category": "x", "tags": [], "aliases": []},
              "scope": "会话记忆", "scope_key": "session"} for i in range(45)]
    chosen, _q, triggered = knowledge._rank_items(items, "量子 比特", allow_model=False)
    assert triggered is False
    assert chosen


def test_llm_pick_default_off(monkeypatch):
    from app import knowledge
    knowledge._PICK_CFG["t"] = 0.0
    monkeypatch.setattr("app.config.load_config", lambda: {})
    assert knowledge._llm_pick_enabled() is False


def test_memory_prompt_lists_newest_nodes():
    """节点是 append 追加的：列表必须取「最近更新的 12 条」。

    取数组前 12 条等于永远只给模型看最旧的，它看不见自己刚写的，
    下一轮又会 add 一条同主题的（NOTE 43.87 的 5 组重复条目就是这么来的）。
    """
    nodes = [{"id": "n%d" % i, "title": "条目 %d" % i, "content": "",
              "updated_at": 1000 + i} for i in range(30)]
    text = mem_tool.build_tool_prompt({"ai_write": True,
                                       "memory": {"nodes": nodes, "edges": []}})
    body = text.split("现有条目")[1]
    assert "条目 29" in body and "条目 18" in body
    assert "条目 17" not in body and "条目 0" not in body


def test_profile_rows_skip_when_guidance_lists(monkeypatch):
    """画像准则模板用 {current_auto} 列了条目 → 「现有条目」里不再重复列一遍"""
    monkeypatch.setattr(mem_tool, "get_profile_kb", lambda: "kb_p")
    monkeypatch.setattr(mem_tool, "load_profile",
                        lambda: {"auto_learn_enabled": True,
                                 "learning_prompt_custom": "当前画像：{current_auto}"})
    monkeypatch.setattr(mem_tool, "load_kb_index",
                        lambda: {"kbs": [{"id": "kb_p", "name": "用户画像"}]})
    monkeypatch.setattr(mem_tool, "load_kb",
                        lambda kid: {"nodes": [{"id": "p1", "title": "画像条目甲"}]})
    text = mem_tool.build_tool_prompt({"ai_write": True})
    assert text.count("画像条目甲") == 1
    assert "画像条目甲" not in text.split("现有条目")[1]


def test_profile_rows_kept_when_guidance_has_no_listing(monkeypatch):
    """模板没写 {current_auto} → 仍要在「现有条目」里列出，否则模型拿不到 id"""
    monkeypatch.setattr(mem_tool, "get_profile_kb", lambda: "kb_p")
    monkeypatch.setattr(mem_tool, "load_profile",
                        lambda: {"auto_learn_enabled": True,
                                 "learning_prompt_custom": "只写规则，不列条目"})
    monkeypatch.setattr(mem_tool, "load_kb_index",
                        lambda: {"kbs": [{"id": "kb_p", "name": "用户画像"}]})
    monkeypatch.setattr(mem_tool, "load_kb",
                        lambda kid: {"nodes": [{"id": "p1", "title": "画像条目甲"}]})
    text = mem_tool.build_tool_prompt({"ai_write": True})
    assert "画像条目甲" in text.split("现有条目")[1]


def test_memory_prompt_widens_listing_when_over_limit(monkeypatch):
    """超过软上限时：多列条目方便合并 + 多出一段「需要整理」的硬要求。"""
    nodes = [{"id": "n%d" % i, "title": "条目 %d" % i, "updated_at": 1000 + i}
             for i in range(40)]
    monkeypatch.setattr(mem_tool, "load_config",
                        lambda: {"memory_soft_limit": 30, "memory_list_recent": 24})
    text = mem_tool.build_tool_prompt({"ai_write": True,
                                       "memory": {"nodes": nodes, "edges": []}})
    assert "本会话记忆需要整理" in text
    body = text.split("现有条目")[1]
    assert "条目 39" in body and "条目 16" in body
    assert "条目 15" not in body

    # 软上限 0 = 不限：不注入整理段，列表回到默认 12 条
    monkeypatch.setattr(mem_tool, "load_config",
                        lambda: {"memory_soft_limit": 0, "memory_list_recent": 24})
    text = mem_tool.build_tool_prompt({"ai_write": True,
                                       "memory": {"nodes": nodes, "edges": []}})
    assert "本会话记忆需要整理" not in text
    body = text.split("现有条目")[1]
    assert "条目 28" in body and "条目 27" not in body
