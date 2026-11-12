# -*- coding: utf-8 -*-
"""系统提示装配（P1-step5b：从 app/api/chat.py 抽出）。

只做「纯装配」：输入会话 / 角色 / 配置 → 输出提示词文本。
不碰 SSE、不碰落库，因此可以单测（见 tests/test_prompt_assembly.py）。

chat.py 侧保留的只是：什么时候推 SSE 事件、把文本接到 messages 的哪一层。
"""
import time

from app import gal, mem_tool
from app.knowledge import pinned_block, retrieve_memory


def _safe_token(v, n=40):
    # 只留 ASCII 的字母数字与 _ - . :；中文/换行/尖括号等一律丢弃，
    # 防止把用户输入的指令性文本注入系统提示。
    return "".join(ch for ch in str(v or "")
                   if ch.isascii() and (ch.isalnum() or ch in "_-.:"))[:n]


def ui_context_line(raw, hall=True):
    """把前端上报的「当前界面」压成一行动态上下文。

    只接受白名单字段（模式 / 模块 / 对局 id / 模拟日 / 标的），
    避免把任意用户文本注入系统提示。hall=False 时完全不注入大厅数据。
    """
    if not hall:
        return ""
    if not isinstance(raw, dict):
        return ""
    if str(raw.get("mode") or "") != "hall":
        return ""
    if _safe_token(raw.get("module"), 24) != "stock":
        return ""
    parts = ["【当前界面】功能大厅 → 模拟股市"]
    gid = _safe_token(raw.get("game_id"), 32)
    cursor = _safe_token(raw.get("cursor_date"), 10)
    symbol = _safe_token(raw.get("symbol"), 12)
    if gid:
        parts.append("对局 %s" % gid)
    if cursor:
        parts.append("模拟日 %s" % cursor)
    if symbol:
        parts.append("正在看 %s" % symbol)
    return ("；".join(parts)
            + "。回答该模块问题时优先调用 stock.*；不要反问用户「你在哪一局 / 哪个标的」，"
              "除非上下文里确实没有这些信息。")


def system_clock_text(now=None):
    """本机系统时间（**每轮现算，不要缓存**）。

    只注入普通对话；GAL 演出不注入——那里剧情内的时间由 stats（如「第 3 天 黄昏」）
    自己管，塞进现实时间会破坏沉浸感。
    """
    t = now or time.localtime()
    base = time.altzone if (t.tm_isdst and hasattr(time, "altzone")) else time.timezone
    total = -int(base) // 60
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return ("【当前时间】%s 星期%s %s（UTC%s%02d:%02d）\n"
            "（这是本轮请求开始时的本机系统时间，每轮都会重新读取；历史消息、记忆、"
            "迁移提示词里的日期只是当时记录，不是现在。被问到「今天几号 / 现在几点 / "
            "今年是哪一年」时，直接以本行为准：不要联网搜索，不要凭训练数据猜，"
            "也不要说「拿不到实时时钟」。如果本行日期晚于你的训练截止时间，这是正常的，"
            "仍以本行为准；除非用户要求精确到秒，否则不要反复追问当前时间。）"
            % (time.strftime("%Y-%m-%d", t), "一二三四五六日"[t.tm_wday],
               time.strftime("%H:%M", t), sign, total // 60, total % 60))


def system_clock_short(now=None):
    """给「本轮用户消息」加的一行时间提示。

    完整时间块仍放在 system 末尾；但长系统提示里它容易被忽略，
    所以再在最后一条 user 消息前压一行短的，回答日期/时间时更难漏看。
    """
    t = now or time.localtime()
    return ("【系统时间】%s 星期%s %s（回答日期 / 时间问题时以此为准，"
            "不要凭训练数据猜，也不要说拿不到实时时钟）"
            % (time.strftime("%Y-%m-%d", t), "一二三四五六日"[t.tm_wday],
               time.strftime("%H:%M", t)))


def tool_prompt(s, include_profile=True, include_kb=True, include_session=True, parts=False):
    """记忆工具说明块：把「你有读写权限 + 可用作用域 + 现有条目」写进提示，
    模型才会在回复里给出写入指令（而不是嘴上说「我无法记录」）。

    include_profile / include_kb / include_session 供 GAL 与会话级开关使用。
    parts=True 返回 (静态说明, 动态条目)：动态条目必须贴到请求末尾，别放进 system。"""
    try:
        return mem_tool.build_tool_prompt(s, include_profile=include_profile,
                                          include_kb=include_kb,
                                          include_session=include_session, parts=parts)
    except Exception as e:
        print(f"[记忆工具] 提示词生成失败：{e}")
        return ("", "") if parts else ""


GLOBAL_RULES_HEAD = "【全局规则（必须遵守；与本轮用户消息冲突时以本轮为准）】"
SCENE_HEAD = "【场景模板（系统默认补充；被全局规则覆盖）】"
EXECUTION_RULES = (
    "\n\n【执行纪律（每轮适用）】\n"
    "1. 回答前先判断：本轮是否推进了全局规则里的目标？偏离就先纠正。\n"
    "2. 规则要求证据时，只使用对话、记忆、工具结果里的事实；没有就说「证据不足」。\n"
    "3. 规则冲突时按 本轮用户消息 > 全局规则 > 场景模板 取舍，并说明选了哪条。"
)


def base_system_parts(s, query, gal_mode=False, profile_manual="", tool_text="", clock="",
                      observations="", skip_profile=False, include_kbs=True, profile_text="",
                      skills_text="", include_session=True):
    """把系统提示拆成「稳定段」与「每轮易变段」，返回 (fixed, dynamic, mem_count)。

    为什么要拆：动态检索结果每轮都不同，一旦它出现在 system 中间，从它开始往后的内容
    （Agent 工具说明那几千 token）**每轮都会 miss 前缀缓存**。调用方应按
    「fixed → Agent 说明 → 界面上下文 → dynamic（检索 / 长期记忆 / 观测）→ 时钟」拼接。
    """
    manual = (profile_manual or "").strip()
    fixed = ((GLOBAL_RULES_HEAD + "\n" + manual) if manual else "")

    # 场景模板：系统默认补充，放在用户规则之后；冲突时以用户规则为准
    try:
        from app import scenes
        scene_text = scenes.prompt_for((s or {}).get("scene"))
    except Exception:
        scene_text = ""
    if scene_text:
        scene_block = SCENE_HEAD + "\n" + scene_text
        fixed = (fixed + "\n\n" + scene_block) if fixed else scene_block

    # Skill 索引（L1）：只给名称与描述，正文由模型按需调用 skill.read 展开。
    if skills_text:
        fixed = (fixed + "\n\n" + skills_text) if fixed else skills_text

    # 只读画像：GAL 勾选「读画像」时由调用方生成；明确标注不要修改
    if profile_text:
        fixed = (fixed + "\n\n" + profile_text) if fixed else profile_text

    # 记忆工具说明：内容随「会话记忆条目」变化而变，但更新频率远低于每轮检索
    if tool_text:
        fixed = (fixed + "\n\n" + tool_text) if fixed else tool_text

    # 记忆索引（L3，对齐 CC 的 MEMORY.md）：长期记忆的标题级常驻清单。
    # 只列标题与一行钩子，正文仍由检索 / memory.search 按需取；本地小窗口模型不注入。
    # 注意：它随记忆写入变化，属于**动态段**（放到 system 里会让后面整段历史每轮 miss）。
    try:
        from app.knowledge import memory_index_block
        idx_text = memory_index_block(
            s, include_profile=False,
            include_kbs=bool(include_kbs), include_session=bool(include_session))
    except Exception as e:                   # noqa: BLE001  索引失败不能拦住对话
        idx_text = ""
        print(f"[记忆索引] 生成失败：{e}")

    # 只有用户真的写了规则时才加执行纪律：没有目标可推进时它只是噪音
    if manual:
        fixed += EXECUTION_RULES

    # ---- 以下都是「每轮易变」段 ----
    dyn = ""
    # 记忆索引：随记忆写入变化，放动态段（贴请求末尾），保住 system 的稳定前缀
    if idx_text:
        dyn = idx_text
    # 记忆检索：从用户画像 + 会话记忆 + 绑定的知识库召回。
    mem_block, mem_count = retrieve_memory(s, query, exclude_pinned=bool(gal_mode),
                                           skip_profile=bool(tool_text) or bool(skip_profile),
                                           include_kbs=bool(include_kbs),
                                           include_session=bool(include_session))
    if mem_block:
        dyn = (dyn + "\n\n" + mem_block) if dyn else mem_block
    else:
        mem_count = 0

    # 统一记忆：只取手动记录与任务结果（知识库检索已在上面完成，避免重复）
    # 会话级「读取记忆」关闭时整块跳过，避免这部分仍然偷偷进提示词。
    mem_long = ""
    if include_session:
        try:
            from app import memory
            mem_long = memory.context_block(s, query)
        except Exception:
            mem_long = ""
    if mem_long:
        dyn = (dyn + "\n\n" + mem_long) if dyn else mem_long

    # 观测摘要由调用方生成（本函数保持纯函数，不碰数据库）
    if observations:
        dyn = (dyn + "\n\n" + observations) if dyn else observations

    # 任务状态：目标 / 验收 / 计划 / 最近改动 / 未解决错误 / 素材与视觉基线。
    # 放在动态段（不破坏稳定前缀缓存），但每轮都在场，避免长对话后目标漂移。
    try:
        from app import task_state
        led_text = task_state.text(s)
    except Exception:
        led_text = ""
    if led_text:
        dyn = (dyn + "\n\n" + led_text) if dyn else led_text

    if clock:                       # 放最后：动态内容不破坏前面稳定段的前缀缓存
        dyn = (dyn + "\n\n" + clock) if dyn else clock
    return fixed, dyn, mem_count


def base_system_prompt(s, query, gal_mode=False, profile_manual="", tool_text="", clock="",
                       observations="", skip_profile=False, include_kbs=True, profile_text="",
                       skills_text="", include_session=True):
    """基础系统提示（稳定段 + 易变段依次拼接）。

    需要自己控制拼接位置时用 base_system_parts —— 默认顺序会把 Agent 工具说明
    排在动态检索之后，缓存命中率更低。
    """
    fixed, dyn, mem_count = base_system_parts(
        s, query, gal_mode=gal_mode, profile_manual=profile_manual, tool_text=tool_text,
        clock=clock, observations=observations, skip_profile=skip_profile,
        include_kbs=include_kbs, profile_text=profile_text, skills_text=skills_text,
        include_session=include_session)
    text = "\n\n".join([x for x in (fixed, dyn) if x])
    return text, mem_count


# 导演纪律：把模型的注意力从「格式正确」拉回「故事好看」。
# 措辞保持中性——不写死任何具体世界观（规则/机制由知识库驱动）。
DIRECTOR_RULES = (
    "\n\n### 导演纪律（每一轮都要做到）\n"
    "- 本轮必须有**实质推进**：关系 / 处境 / 局势 / 目标至少有一项发生变化。\n"
    "- 不要原地打转、不要只寒暄、不要连续两轮演同一种场面。\n"
    "- 按「硬性规则 / 世界设定」里的**核心机制**推进：该出现的机制要真的出现（别当背景板），"
    "关键抉择要真的改变局势。\n"
    "- 先在心里定下本轮的冲突点，可在 JSON 里加一个 \"beat\" 字段记下它（一句话）。"
)


def gal_round_prompt(gal_char, s, gal_node=None, *, gal_auto=False, emotion_multi=False,
                     agent_on=False, fx=None, mem_on=False, include_kbs=True):
    """GAL 演出提示 = 演出契约 + 当前剧情节点 + 全剧走向 + 规则常驻 + 导演纪律 + 前情提要"""
    gp = gal.build_prompt(gal_char, bool(emotion_multi), agent_on, fx,
                          stage={"stats": s.get("gal_stats") or [],
                                 "bg": s.get("gal_bg") or "",
                                 "bgm": s.get("gal_bgm") or ""},
                          mem_on=bool(mem_on))

    if gal_node:
        gp += "\n\n" + gal.build_node_prompt(gal_node, int(s.get("gal_round") or 0))

    if gal_auto:
        # AI 自动剧本：额外给出全剧走向，让每轮演出对齐整条故事线
        gp += ("\n\n### 本段故事由你（AI）自行编剧\n"
               "上面「剧情节点」就是当前这一幕的演出要求，请在不脱离角色设定的前提下主动推动它；"
               "不要凭空跳过尚未发生的情节。")
        if s.get("gal_auto_outline"):
            gp += "\n\n### 全剧走向（供你把控节奏，不要直接说出来）\n" + str(s["gal_auto_outline"])

    # 规则常驻：世界规则不能只靠关键词检索命中，必须每轮都在场
    try:
        pin_text, _pin_n = pinned_block(s, include_kbs=include_kbs)
    except Exception as e:
        pin_text = ""
        print(f"[GAL] 规则常驻注入失败：{e}")
    if pin_text:
        gp += "\n\n" + pin_text

    gp += DIRECTOR_RULES

    # 前情提要：每幕结束时刷新，之后常驻注入，避免长线跑偏
    if s.get("gal_recap"):
        gp += "\n\n### 前情提要（已经发生过的事，务必与之连贯）\n" + str(s["gal_recap"])
    return gp
