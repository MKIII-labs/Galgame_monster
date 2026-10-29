# -*- coding: utf-8 -*-
"""场景包：规范化、提示词、能力排序、意图路由"""
from app import scenes


def test_normalize():
    assert scenes.normalize("OFFICE") == "office"
    assert scenes.normalize("nope") == "general"
    assert scenes.normalize(None) == "general"


def test_prompt_and_caps():
    assert "结论" in scenes.prompt_for("office")
    assert scenes.prompt_for("general") == ""
    ordered = scenes.filter_caps("office", ["memory.search", "task.create", "x.other"])
    assert ordered[0] in ("calendar.create", "task.create", "memory.search")
    assert "x.other" in ordered


def test_detect():
    assert scenes.detect("帮我准备明天的客户会议材料") == "office"
    assert scenes.detect("这道题怎么理解，考试会考吗") == "study"
    assert scenes.detect("周末想出去旅行，顺便吃顿好的") == "life"
    assert scenes.detect("你好") == "general"


def test_profile_precedes_scene(monkeypatch):
    from app import memory, prompt_assembly as pa
    monkeypatch.setattr(pa, "retrieve_memory", lambda *a, **k: ("", 0))
    monkeypatch.setattr(memory, "context_block", lambda *a, **k: "")
    text, _ = pa.base_system_prompt({"scene": "office"}, "问题",
                                    profile_manual="回答必须一句话")
    assert text.index("回答必须一句话") < text.index("【场景模板")
