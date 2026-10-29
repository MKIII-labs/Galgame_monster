# -*- coding: utf-8 -*-
"""GAL 回复解析：契约容错与字段归一（纯函数，无 IO）"""

from app import gal

FULL = """```json
{"dialog":[{"speaker":"NARR","speech":"夜色很沉。"},
           {"speaker":"林羽","speech":"你来了。","emotion":"平静"}],
 "options":[{"text":"走进去"}],
 "bg":"夜晚的海边","bgm":"紧张",
 "cast":[1],
 "map":{"place":"码头","marks":[{"name":"你","x":0.5,"y":0.5}]},
 "stats":[{"key":"好感度","value":10,"max":100}]}
```"""


def test_parse_reply_rejects_empty_and_plain_text():
    assert gal.parse_reply("") is None
    assert gal.parse_reply("   ") is None
    # 纯散文不是合法契约 → None（上层改走兜底分段）
    assert gal.parse_reply("夜色很沉，你来了。") is None


def test_parse_reply_full_contract():
    d = gal.parse_reply(FULL, "林羽")
    assert d is not None
    assert d["bg"] == "夜晚的海边"
    assert d["bgm"] == "紧张"
    assert d["cast"] == [1]
    assert d["map"]["place"] == "码头"
    assert len(d["dialog"]) == 2
    assert d["options"][0]["text"] == "走进去"
    assert d["stats"][0]["key"] == "好感度"
    assert d["nudge"] == ""          # 本轮没写 nudge → 空串（前端改用兜底文案）


def test_nudge_passthrough():
    """nudge：本轮没有选项时，模型给的一句「邀请玩家开口」的话；要能透传并做长度/空白收敛"""
    d = gal.parse_reply('{"dialog":[{"speech":"x"}],"nudge":"  （她等着你开口）  "}')
    assert d["nudge"] == "（她等着你开口）"
    assert gal.parse_reply('{"dialog":[{"speech":"x"}]}')["nudge"] == ""
    assert len(gal.parse_reply('{"dialog":[{"speech":"x"}],"nudge":"%s"}' % ("啊" * 200))["nudge"]) == 80


def test_cast_三态():
    """未声明=None（保持现状）｜[]=清空｜数组=去重取正整数、最多 3 个"""
    assert gal.parse_reply(
        '{"dialog":[{"speech":"x"}]}')["cast"] is None
    assert gal.parse_reply(
        '{"dialog":[{"speech":"x"}],"cast":[]}')["cast"] == []
    assert gal._norm_cast(2) == [2]
    assert gal._norm_cast([1, 2, 2, 3, 4]) == [1, 2, 3]
    assert gal._norm_cast(["a", None, 0, -1]) == []
    assert gal._norm_cast("x") == []


def test_num01_clamp():
    assert gal._num01("abc") is None
    assert gal._num01(None) is None
    assert gal._num01(1.5) == 1.0
    assert gal._num01(-1) == 0.0
    assert gal._num01(0.25) == 0.25


def test_norm_map_planes_and_legacy_pos():
    m = gal._norm_map({"place": "地铁站",
                       "marks": [{"name": "你", "x": 0.5, "y": 0.6},
                                 {"name": "出口", "pos": "北"}],
                       "links": [{"a": "你", "b": "出口", "label": "对视"},
                                 {"a": "你", "b": "不存在", "label": "x"}]})
    assert m["place"] == "地铁站"
    assert len(m["marks"]) == 2
    # 旧九宫格「北」应换算成坐标
    assert m["marks"][1]["x"] > 0.4 and m["marks"][1]["y"] < 0.2
    # 端点不存在的连线要被过滤
    assert m["links"] == [{"a": "你", "b": "出口", "label": "对视"}]
    assert gal._norm_map({}) is None
    assert gal._norm_map("x") is None


def test_joined_speech():
    d = gal.parse_reply(FULL, "林羽")
    assert gal.joined_speech(d["dialog"]) == "你来了。"


def test_parse_plan_and_outline_text():
    raw = '{"title":"剧名X","acts":[{"title":"幕1","summary":"情境｜目标｜阻碍｜转折｜代价"},' \
          '{"title":"幕2","summary":"s2"},{"title":"幕3","summary":"s3"}]}'
    plan = gal.parse_plan(raw, 5)
    assert plan["title"] == "剧名X"
    assert len(plan["acts"]) == 3
    # 超出 acts 上限要被截断
    assert len(gal.parse_plan(raw, 2)["acts"]) == 2
    assert gal.parse_plan("不是 JSON", 5) is None
    txt = gal.plan_outline_text(plan)
    assert "剧名：剧名X" in txt and "第 1 幕 幕1" in txt


def test_parse_next_act_and_append():
    node = gal.parse_next_act('{"act":{"title":"第二幕","prompt":"推进","maxRounds":3}}', "a2", 4)
    assert node["id"] == "a2" and node["type"] == "limited_turn"
    assert node["prompt"].startswith("【第二幕】")
    assert node["maxRounds"] == 3
    end = gal.parse_next_act('{"ending":true,"prompt":"收束"}', "a9", 4)
    assert end["type"] == "ending"
    assert gal.parse_next_act("不是 JSON", "a3", 4) is None

    st = gal.start_story({"title": "T", "acts": [{"title": "幕1", "summary": "s1"}]}, 4)
    assert gal.acts_done(st) == 1
    st2 = gal.append_node(st, node)
    assert gal.acts_done(st2) == 2
    # 上一幕的 transitions 要指向新幕
    assert gal.story_node(st2, "a1")["transitions"] == ["a2"]


def test_story_title_survives_append():
    """防回归：append_node 里 normalize_story() 会丢 title，必须先在归一化前取出来"""
    st = gal.start_story({"title": "T", "acts": [{"title": "幕1", "summary": "s1"}]}, 4)
    node = gal.parse_next_act('{"act":{"title":"第二幕","prompt":"推进"}}', "a2", 4)
    assert gal.append_node(st, node).get("title") == "T"
    # 空剧本走另一条早返回分支，同样不能丢
    only = {"start": "a1", "nodes": [], "title": "T2"}
    assert gal.append_node(only, node).get("title") == "T2"


def test_apply_stats_new_and_accumulate():
    items, changed = gal.apply_stats([], [{"key": "生命力", "value": 50, "max": 100, "icon": "heart"}])
    assert changed and items[0]["value"] == 50
    items, changed = gal.apply_stats(items, [{"key": "生命力", "delta": -20}])
    assert changed and items[0]["value"] == 30
    # 夹取：不得越界
    items, _ = gal.apply_stats(items, [{"key": "生命力", "delta": 999}])
    assert items[0]["value"] == 100
    # 只声明无值 → 不建空条
    items2, changed2 = gal.apply_stats([], [{"key": "空条"}])
    assert items2 == [] and changed2 is False


def test_apply_stats_merges_by_label_when_key_renamed():
    """防回归：模型把「生命力」的 key 从「生命」改成「生命力」，不能攒出两条"""
    items, changed = gal.apply_stats(
        [{"key": "生命", "label": "生命力", "value": 84, "max": 100}],
        [{"key": "生命力", "label": "生命力", "value": 40, "max": 100}])
    assert changed is True
    assert len(items) == 1
    assert items[0]["value"] == 40          # 后者覆盖
    assert items[0]["key"] == "生命"         # 保留首次的 key，UI 不会来回改名


def test_apply_stats_cleans_existing_duplicates():
    """已经攒下的重复要能自愈（列表靠后的覆盖靠前的）"""
    cur = [{"key": "生命", "label": "生命力", "value": 84, "max": 100},
           {"key": "危险", "label": "危险度", "value": 30, "max": 100},
           {"key": "危险度", "label": "危险度", "value": 72, "max": 100},
           {"key": "生命力", "label": "生命力", "value": 40, "max": 100}]
    items, changed = gal.apply_stats(cur, [])
    assert changed is True
    assert [i["label"] for i in items] == ["生命力", "危险度"]
    assert items[0]["value"] == 40 and items[1]["value"] == 72


def test_apply_stats_delta_still_accumulates_after_rename():
    cur = [{"key": "生命", "label": "生命力", "value": 50, "max": 100}]
    items, _ = gal.apply_stats(cur, [{"key": "生命力", "label": "生命力", "delta": -20}])
    assert items[0]["value"] == 30


def test_roll_detail_readable():
    r = gal.do_roll({"why": "撬锁", "dc": 12, "mod": 2})
    assert r and "d20" in r["detail"] and "需 ≥ 12" in r["detail"]
    assert ("成功" in r["detail"]) == r["success"]
    r2 = gal.do_roll({"why": "爆炸", "dice": "2d6+1"})
    assert r2 and "2d6" in r2["detail"] and "合计" in r2["detail"]
    r3 = gal.do_roll({"why": "掷骰", "dice": "1d6"})
    assert r3 and "加值" not in r3["detail"]      # 无加值时不该出现「加值」段
    r4 = gal.do_roll({"why": "裸检定", "dc": 10})  # 无 mod
    assert r4 and "加值" not in r4["detail"]


def test_normalize_stat_invalid():
    assert gal.normalize_stat("x") is None
    assert gal.normalize_stat({}) is None
    assert gal.normalize_stat({"key": "时间", "value": "第 3 天 黄昏"})["value"] == "第 3 天 黄昏"


def test_extract_effects_removes_keyword():
    fx = [{"keyword": "闪光", "css": "flash", "audio": "", "mode": "after"}]
    out = gal.extract_effects("他抬起头*闪光*笑了", fx)
    assert isinstance(out, tuple) and len(out) == 2
    text = out[0]
    assert "闪光" not in text and "他抬起头" in text
