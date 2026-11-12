# -*- coding: utf-8 -*-
"""提示词装配（P1-step5b 从 api/chat.py 抽出）：纯函数，可直接单测。"""
import pytest

from app import agent
from app import prompt_assembly as pa


NEUTRAL_CHAR = {"name": "测试角色", "profile": "普通设定。"}


@pytest.fixture
def fake_kb(monkeypatch):
    """把回忆检索 / 规则常驻换成可控桩"""
    monkeypatch.setattr(pa, "retrieve_memory",
                        lambda s, q, exclude_pinned=False, skip_profile=False,
                        include_kbs=True, include_session=True: ("【回忆】某条记忆", 1))
    monkeypatch.setattr(pa, "pinned_block",
                        lambda s, include_kbs=True: ("【硬性规则】某条规则", 2))
    return monkeypatch


def test_base_system_prompt_composes_all_parts(fake_kb):
    text, n = pa.base_system_prompt({"kb_ids": []}, "问题", profile_manual="手动偏好",
                                    tool_text="工具说明")
    assert n == 1
    # 易变段（检索）放在稳定段（工具说明）之后，保护前缀缓存
    assert text.index("手动偏好") < text.index("工具说明") < text.index("【回忆】")


def test_stable_prefix_survives_volatile_blocks(monkeypatch):
    """两次请求如果只有检索 / 观测 / 时钟不同，公共前缀必须覆盖到执行纪律。

    这是前缀缓存的回归测试：检索结果一旦混进稳定段中间，
    后面几千字的工具说明每轮都会失效。
    """
    monkeypatch.setattr(pa, "retrieve_memory",
                        lambda s, q, exclude_pinned=False, skip_profile=False,
                        include_kbs=True, include_session=True: ("【回忆】" + q, 1))
    a, _ = pa.base_system_prompt({}, "问题一", profile_manual="规则", tool_text="工具说明",
                                 observations="【相关观测】A", clock="【当前时间】A")
    b, _ = pa.base_system_prompt({}, "问题二", profile_manual="规则", tool_text="工具说明",
                                 observations="【相关观测】B", clock="【当前时间】B")
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    assert "工具说明" in a[:common]
    assert "【执行纪律" in a[:common]


def test_base_system_prompt_without_memory_zeroes_count(monkeypatch):
    monkeypatch.setattr(pa, "retrieve_memory",
                        lambda s, q, exclude_pinned=False, skip_profile=False, include_kbs=True, include_session=True: ("", 0))
    text, n = pa.base_system_prompt({}, "问", profile_manual="只有偏好")
    assert n == 0
    assert text.startswith(pa.GLOBAL_RULES_HEAD + "\n只有偏好")
    assert pa.EXECUTION_RULES in text


def test_skip_session_memory_does_not_read_unified_memory(monkeypatch):
    """会话级「读取记忆」关闭时：不召回会话记忆，也不读统一记忆。"""
    called = []

    def fake_retrieve(s, q, exclude_pinned=False, skip_profile=False,
                      include_kbs=True, include_session=True):
        return ("" if not include_session else "【回忆】", 0 if not include_session else 1)

    monkeypatch.setattr(pa, "retrieve_memory", fake_retrieve)
    from app import memory
    monkeypatch.setattr(memory, "context_block",
                        lambda s, q: called.append(1) or "【统一记忆】")

    fixed, dyn, n = pa.base_system_parts({}, "问", include_session=False, include_kbs=False)
    assert n == 0 and "【统一记忆】" not in dyn and not called


def test_global_rules_label_and_scene_subordination(monkeypatch):
    """全局规则带固定标签，场景模板明确标注「被全局规则覆盖」且排在其后"""
    monkeypatch.setattr(pa, "retrieve_memory",
                        lambda s, q, exclude_pinned=False, skip_profile=False, include_kbs=True, include_session=True: ("", 0))
    text, _ = pa.base_system_prompt({"scene": "study"}, "问题",
                                    profile_manual="你是我的老师")
    assert text.startswith(pa.GLOBAL_RULES_HEAD + "\n你是我的老师")
    assert pa.SCENE_HEAD in text
    assert "被全局规则覆盖" in text
    assert text.index("你是我的老师") < text.index(pa.SCENE_HEAD)


def test_execution_rules_only_when_user_rules_exist(monkeypatch):
    """没有用户规则时不注入执行纪律：没有目标可推进，它只是噪音"""
    monkeypatch.setattr(pa, "retrieve_memory",
                        lambda s, q, exclude_pinned=False, skip_profile=False, include_kbs=True, include_session=True: ("", 0))
    with_rules, _ = pa.base_system_prompt({"scene": "study"}, "问", profile_manual="规则")
    assert "【执行纪律（每轮适用）】" in with_rules
    without, _ = pa.base_system_prompt({"scene": "study"}, "问")
    assert "执行纪律" not in without


def test_observations_appended_before_clock(monkeypatch):
    """观测摘要是动态内容：排在执行纪律之后、时钟之前"""
    monkeypatch.setattr(pa, "retrieve_memory",
                        lambda s, q, exclude_pinned=False, skip_profile=False, include_kbs=True, include_session=True: ("", 0))
    text, _ = pa.base_system_prompt({}, "q", profile_manual="规则",
                                    observations="【相关观测】X", clock="【当前时间】Y")
    assert text.index("【执行纪律") < text.index("【相关观测】X") < text.index("【当前时间】Y")


def test_base_system_prompt_empty_everything(monkeypatch):
    monkeypatch.setattr(pa, "retrieve_memory",
                        lambda s, q, exclude_pinned=False, skip_profile=False, include_kbs=True, include_session=True: ("", 0))
    assert pa.base_system_prompt({}, "") == ("", 0)


def test_base_system_prompt_passes_exclude_pinned(fake_kb):
    seen = {}

    def spy(s, q, exclude_pinned=False, skip_profile=False, include_kbs=True,
            include_session=True):
        seen["v"] = exclude_pinned
        return "", 0
    fake_kb.setattr(pa, "retrieve_memory", spy)
    pa.base_system_prompt({}, "q", gal_mode=True)
    assert seen["v"] is True                 # GAL 模式下要排除常驻规则，避免重复
    pa.base_system_prompt({}, "q", gal_mode=False)
    assert seen["v"] is False


def test_base_system_prompt_skips_profile_when_tool_text(fake_kb):
    """有「记忆工具说明」时不再单独列画像条目（同批条目已由其列出，去重省 token）"""
    seen = {}

    def spy(s, q, exclude_pinned=False, skip_profile=False, include_kbs=True,
            include_session=True):
        seen["skip"] = skip_profile
        return "", 0
    fake_kb.setattr(pa, "retrieve_memory", spy)
    pa.base_system_prompt({}, "q", tool_text="工具说明")
    assert seen["skip"] is True
    pa.base_system_prompt({}, "q")
    assert seen["skip"] is False


def test_tool_prompt_swallows_error(monkeypatch):
    def boom(s):
        raise RuntimeError("坏了")
    monkeypatch.setattr(pa.mem_tool, "build_tool_prompt", boom)
    assert pa.tool_prompt({}) == ""          # 出错不能把整轮聊天带崩


def test_gal_round_prompt_contains_all_sections(fake_kb, monkeypatch):
    monkeypatch.setattr(pa.gal, "build_prompt",
                        lambda *a, **k: "【契约】舞台: %s" % k.get("stage"))
    monkeypatch.setattr(pa.gal, "build_node_prompt", lambda node, r: "【节点】%s/%s" % (node["id"], r))
    s = {"gal_stats": [{"key": "生命", "value": 40}], "gal_bg": "bg.jpg", "gal_bgm": "b",
         "gal_round": 2, "gal_auto_outline": "全剧走向XYZ", "gal_recap": "前情ABC"}
    gp = pa.gal_round_prompt(NEUTRAL_CHAR, s, {"id": "a1"}, gal_auto=True,
                             emotion_multi=True, agent_on=True, fx=[], mem_on=True)
    for kw in ("【契约】", "【节点】a1/2", "自行编剧", "全剧走向XYZ", "【硬性规则】",
               "导演纪律", "实质推进", "前情ABC"):
        assert kw in gp, "GAL 提示缺 %s" % kw


def test_gal_round_prompt_minimal(fake_kb):
    gp = pa.gal_round_prompt(NEUTRAL_CHAR, {})
    assert gp                                     # 没有节点/剧本也不能是空串
    assert "本段故事由你" not in gp               # 非自动剧本时不出现
    assert "前情提要" not in gp                   # 没有 recap 时不出现
    assert "导演纪律" in gp                       # 但导演纪律永远在


def test_gal_round_prompt_survives_pinned_error(monkeypatch):
    monkeypatch.setattr(pa, "pinned_block",
                        lambda s, include_kbs=True: (_ for _ in ()).throw(RuntimeError("炸")))
    gp = pa.gal_round_prompt(NEUTRAL_CHAR, {})
    assert "导演纪律" in gp                       # 常驻注入失败也要能继续演出


# ============================================================
# 系统时间（新增能力：模型必须知道「现在是什么时间」）
# ============================================================

def test_system_clock_text_has_date_and_weekday():
    import time
    t = pa.system_clock_text()
    assert "【当前时间】" in t
    assert time.strftime("%Y-%m-%d") in t         # 当天日期
    assert "星期" in t
    assert "UTC" in t


def test_system_clock_short_is_compact_and_authoritative():
    import time
    t = pa.system_clock_short()
    assert t.startswith("【系统时间】")
    assert time.strftime("%Y-%m-%d") in t
    assert "不要凭训练数据猜" in t
    assert len(t) < 120                  # 贴在用户消息前，别把正文挤走


def test_system_clock_goes_last(monkeypatch):
    """时间戳放在 system 提示**末尾**（不再是最前）。

    原因：它是每轮都变的动态内容，放最前会让整段 system 提示的前缀每轮都变——
    前缀缓存从首字节开始连续比对，开头一变后面全不命中。移到末尾后，
    前面稳定段（手动偏好 / 检索结果 / 工具说明）可继续吃缓存。位置后移不影响模型读到时间。
    """
    monkeypatch.setattr(pa, "retrieve_memory",
                        lambda s, q, exclude_pinned=False, skip_profile=False, include_kbs=True, include_session=True: ("", 0))
    text, _ = pa.base_system_prompt({}, "q", profile_manual="手动偏好", clock="【当前时间】X")
    assert text.endswith("【当前时间】X")
    assert text.index("手动偏好") < text.index("【当前时间】X")


def test_system_clock_off_by_default(monkeypatch):
    monkeypatch.setattr(pa, "retrieve_memory",
                        lambda s, q, exclude_pinned=False, skip_profile=False, include_kbs=True, include_session=True: ("", 0))
    text, _ = pa.base_system_prompt({}, "q", profile_manual="只有偏好")
    assert "【当前时间】" not in text             # 不传 clock 时不注入（GAL 就是这么调的）


def test_system_clock_treats_itself_as_authoritative():
    text = pa.system_clock_text()
    assert "每轮" in text
    assert "历史消息" in text
    assert "不要联网搜索" in text
    assert "训练截止" in text
    assert "不要反复追问当前时间" in text


def test_search_prompt_does_not_ask_model_to_search_clock():
    text = agent._SEARCH_PROMPT
    assert "当前日期" in text
    assert "【当前时间】" in text
    assert "「最新 / 现在 / 今天 / 今年」" not in text


def test_reminder_prompt_uses_current_round_clock():
    text = agent._REMINDER_PROMPT
    assert "本轮【当前时间】" in text
    assert "历史消息" in text
    assert "重新计算" in text


def test_reminder_prompt_proactively_lands_review_plan():
    text = agent._REMINDER_PROMPT
    assert "主动落地" in text
    assert "calendar.create" in text
    assert "不要只口头答应" in text
    assert "明早" in text
    assert "工作台" in text
    assert "只是陈述事实" in text


def test_capability_prompt_lands_concrete_future_plans():
    from app import agent, httpd  # noqa: F401  导入即注册能力
    text = agent.build_agent_prompt({}, search_ready=False)
    assert "未来安排要主动落地" in text
    assert "calendar.create" in text
    assert "08:00 / 20:00" in text
    assert "不要反问到用户自己动手" in text


def test_prompt_forbids_pseudo_tool_tags_and_empty_claims():
    """提示词里不能再出现任何伪标签写法（写出来等于给模型模板）。

    原生 tools 可用时，连 ```tool 示例也不给：只留一句纪律。
    """
    from app import agent, httpd  # noqa: F401  导入即注册能力
    native = agent.build_agent_prompt({}, search_ready=False, native_tools=True)
    assert "<tool" not in native
    assert "```tool" not in native
    assert '{"calls":' not in native
    assert "原生 function calling" in native
    # 文本协议兜底仍要保留（服务商不支持原生 tools 时靠它工作）
    text = agent.build_agent_prompt({}, search_ready=False, native_tools=False)
    assert "```tool" in text
    assert "<tool调用>" not in text
    assert "<tool>" not in text


def test_kb_custom_prompt_injected_for_writable_kb(monkeypatch):
    from app import mem_tool
    fake = {"id": "kb_math", "name": "数学", "prompt": "只写入数学内容。", "nodes": []}
    monkeypatch.setattr(mem_tool, "writable_scopes", lambda s, **kw: [("kb_math", "数学")])
    monkeypatch.setattr(mem_tool, "load_kb",
                        lambda kid: fake if kid == "kb_math" else None)
    text = mem_tool.build_tool_prompt({})
    assert "知识库写入规则：数学" in text
    assert "ID：kb_math" in text
    assert "scope=kb_math" not in text
    assert "不要带 `scope=` 前缀" in text
    assert "只写入数学内容" in text


def test_kb_custom_prompt_skipped_when_empty(monkeypatch):
    from app import mem_tool
    fake = {"id": "kb_math", "name": "数学", "prompt": "  ", "nodes": []}
    monkeypatch.setattr(mem_tool, "writable_scopes", lambda s, **kw: [("kb_math", "数学")])
    monkeypatch.setattr(mem_tool, "load_kb",
                        lambda kid: fake if kid == "kb_math" else None)
    text = mem_tool.build_tool_prompt({})
    assert "知识库写入规则" not in text


# ============================================================
# 工具提示：联网搜索改由主模型自主调用后，开关是否讲清楚
# ============================================================

def test_agent_prompt_offers_web_search_when_ready():
    from app import agent
    p = agent.build_agent_prompt({}, search_ready=True)
    assert "web_search" in p
    assert "不该搜" in p                          # 治「乱搜」的关键：明确列出不该搜的场景


def test_agent_prompt_denies_search_when_unavailable():
    from app import agent
    p = agent.build_agent_prompt({}, search_ready=False)
    assert "web_search" not in p
    assert "不能" in p and "不要编造" in p        # 不能联网时明确告知，避免假装查过


# ============================================================
# 记忆工具：追加式笔记（append_note）的使用边界
# ============================================================

def test_append_note_guidance_in_prompt():
    """提示词必须讲清「何时追加」以及「不要用 add 覆盖来假装追加」"""
    from app import mem_tool
    text = mem_tool.build_tool_prompt({})
    assert "append_note" in text
    assert "保留原文与历史" in text
    assert "不要用 add 覆盖同名条目来假装追加" in text
    assert "追加要写增量" in text


def test_chat_ops_summary_counts_append_note(monkeypatch):
    """只做追加时，前端汇总要显示「追加 N」而不是空字符串"""
    from app import mem_tool
    sess = {"id": "s1", "memory": {"nodes": [], "edges": []}}
    monkeypatch.setattr(mem_tool, "writable_scopes", lambda s, **kw: [("session", "本会话记忆")])
    monkeypatch.setattr(mem_tool, "load_sessions", lambda: {"sessions": [sess]})
    monkeypatch.setattr(mem_tool, "find_session", lambda data, sid: sess)
    monkeypatch.setattr(mem_tool, "apply_memory_ops",
                        lambda sid, allowed, ops: ({"append_note": 1}, [], {}))
    out = mem_tool.apply_chat_ops("s1", [{"op": "append_note", "content": "补充"}])
    assert out and out["summary"] == "追加 1"


def test_memory_tool_prompt_splits_static_and_dynamic():
    """记忆条数 / 现有条目属于动态段：留在 system 里会让写一次记忆就整段 miss。"""
    from app import mem_tool
    static, dyn = mem_tool.build_tool_prompt({"ai_write": True}, parts=True)
    assert "## 记忆工具" in static
    assert "现有条目" not in static and "本会话记忆现有" not in static
    assert "本会话记忆现有" in dyn and "现有条目" in dyn
    # 不带 parts 时行为不变（仍是完整文本）
    assert "现有条目" in mem_tool.build_tool_prompt({"ai_write": True})


def test_memory_index_goes_to_dynamic_tail(monkeypatch):
    """L3 记忆索引随记忆写入变化，必须落在 dyn（贴请求末尾），不能进 fixed。"""
    from app import knowledge
    monkeypatch.setattr(knowledge, "memory_index_block", lambda *a, **k: "【记忆索引】X")
    fixed, dyn, _n = pa.base_system_parts({}, "问", include_session=False,
                                          include_kbs=False)
    assert "【记忆索引】X" not in fixed
    assert "【记忆索引】X" in dyn


def test_round_note_appends_to_tail_not_system():
    """研究台账 / 缺口提醒贴请求末尾，绝不改写 messages[0]。"""
    from app.api import chat
    msgs = [{"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
            {"role": "assistant", "content": "A"},
            {"role": "user", "content": "TOOL_RESULT"}]
    chat._attach_round_note(msgs, "【研究台账】")
    assert msgs[0]["content"] == "S"
    assert msgs[-1]["content"].endswith("【研究台账】")
