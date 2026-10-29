# -*- coding: utf-8 -*-
"""提示词装配的结构断言。
重点：防「把某个具体世界观写死进代码」这类耦合回归（本会话真实踩过）。"""
from app import gal

# 中性角色：profile 里不含任何具体世界观名词，便于检出「代码里写死」
NEUTRAL_CHAR = {"name": "测试角色", "profile": "一个普通的角色设定，与任何具体作品无关。"}
NODE = {"id": "a1", "type": "limited_turn", "maxRounds": 4, "prompt": "测试幕"}

# 严禁出现在**代码模板**里的「具体世界观」名词
BANNED = ("选择光幕", "光幕", "非凡者", "序列九", "解析系统")

# build_prompt 会把「可用背景 / BGM / 当前舞台现状」按**用户数据**拼进去，
# 那些段落的词来自 data/，不代表代码耦合 —— 检查前先剥离。
_CUT_MARKS = ("### 可用背景", "### 可用 BGM", "### 当前舞台现状")


def _template_only(text):
    cut = len(text)
    for m in _CUT_MARKS:
        i = text.find(m)
        if i >= 0:
            cut = min(cut, i)
    return text[:cut]


def _all_prompts():
    return {
        "build_prompt": _template_only(gal.build_prompt(NEUTRAL_CHAR)),
        "build_script_prompt": gal.build_script_prompt(NEUTRAL_CHAR, world="（世界设定占位）"),
        "build_next_act_prompt": gal.build_next_act_prompt(NEUTRAL_CHAR, world="（世界设定占位）"),
        "build_node_prompt": gal.build_node_prompt(NODE, 1),
        "build_recap_prompt": gal.build_recap_prompt("上次提要", "最近对话"),
    }


def test_no_hardcoded_world_nouns():
    for name, text in _all_prompts().items():
        for bad in BANNED:
            assert bad not in text, "%s 里写死了世界观名词「%s」——应交给知识库驱动" % (name, bad)


def test_build_prompt_keeps_json_contract():
    p = gal.build_prompt(NEUTRAL_CHAR)
    for kw in ("## GAL 演出模式", "dialog", "options", "bg", "bgm", "cast", "map", "stats"):
        assert kw in p, "演出契约缺少 %s" % kw
    assert "### 舞台状态参数" in p and "### 判定" in p


def test_script_prompt_requires_five_elements():
    p = gal.build_script_prompt(NEUTRAL_CHAR)
    for kw in ("情境", "目标", "阻碍", "转折", "代价与钩子"):
        assert kw in p, "分幕大纲缺少要素 %s" % kw
    assert '"acts"' in p


def test_world_is_injected_as_hard_constraint():
    p = gal.build_script_prompt(NEUTRAL_CHAR, world="世界A：规则X")
    assert "### 世界设定（硬约束" in p and "规则X" in p
    p2 = gal.build_next_act_prompt(NEUTRAL_CHAR, world="世界A：规则X")
    assert "### 世界设定（硬约束" in p2 and "规则X" in p2


def test_node_prompt_directs_progress():
    p = gal.build_node_prompt(NODE, 1)
    assert "实质推进" in p
    assert "1 / 4" in p          # 回合进度回灌
    end = gal.build_node_prompt({"id": "a9", "type": "ending", "maxRounds": 0, "prompt": "x"}, 0)
    assert "终章" in end


def test_recap_prompt_carries_previous():
    p = gal.build_recap_prompt("上次提要内容", "最近对话内容")
    assert "前情提要" in p and "上次提要内容" in p and "最近对话内容" in p


def test_speaker_is_a_hard_requirement():
    """对白必须逐段标注「谁在说」。
    本会话实测：347 个对白段里 100 个（29%）没写 speaker → 前端对空 speaker 不显示任何名字，
    玩家分不清是谁的台词。故这一条从「可选」升级为硬要求，别改回去。"""
    p = gal.build_prompt(NEUTRAL_CHAR)
    assert "务必逐段标注" in p
    assert "没有归属的台词是不允许的" in p
    assert "`speaker`（可选）" not in p
