# -*- coding: utf-8 -*-
"""收尾提醒 / 完成声明核对 / 能力域判定：从 app/api/chat.py 抽出的纯函数。

这些都是「看一段文本 + 一点统计，决定要不要提醒模型」的逻辑，不碰请求状态、
不写会话。chat.py 从这里回导，外部 `from app.api.chat import _needs_ask_nudge`
之类的旧路径继续可用。
"""
import re

from app import agent, llm, mem_tool, stepctx


# 工具循环的两条「收尾提醒」。
# 以前遇到这两种情况会**静默 break**：这一轮就以「模型刚说要读文件」结束，
# 用户看到的就是「回答读到一半被打断」（会话里真实踩过，见 docs/NOTES.md）。
_MSG_TOOLS_SPENT = (
    "（系统）工具调用轮次已用尽，**不要再请求任何工具**。"
    "请立刻基于你已经读到的内容，给出最终的、完整的回答；"
    "没读到或不确定的部分如实说明。")
_MSG_BAD_TOOL = (
    "（系统）你刚才的工具调用**格式无法识别**，没有执行。"
    "还需要读文件的话，请严格按这个格式重发：\n"
    "```tool\n"
    '{"calls":[{"tool":"read_file","args":{"path":"相对路径"}}]}\n'
    "```\n"
    "；或者不再调用工具，直接用文字回答。")
_MSG_MEMORY_MISSING = (
    "（系统）你上一段说已经修改 / 删除了记忆，但本轮没有输出任何 ```memory 块，"
    "系统没有执行任何记忆操作。\n"
    "现在只做一件事：如果需要改，立刻在回复末尾输出完整的 ```memory 块"
    '（{"ops":[...]}）；如果不需要改，就直接说「没有改动」，'
    "不要再用「已改 / 现在真的改」这种说法。")
_MSG_MEMORY_MISSING_NATIVE = (
    "（系统）你上一段说已经修改 / 删除了记忆，但本轮没有调用 memory_apply，"
    "系统没有执行任何记忆操作。\n"
    "现在只做一件事：如果需要改，立刻调用 memory_apply（参数 ops 为操作数组）；"
    "如果不需要改，就直接说「没有改动」，不要再用「已改 / 现在真的改」这种说法。")
_MSG_MEMORY_BAD_JSON = (
    "（系统）你刚才的 ```memory 块已闭合，但内容不是合法 JSON，系统没有执行任何记忆操作。"
    "请重新输出合法的 ```memory 块（字符串里的换行写成 \\n，末尾不要多余逗号）；"
    "如果不需要改，就直接说「没有改动」。")
_MSG_MEMORY_BAD_JSON_NATIVE = (
    "（系统）你刚才输出的记忆块内容不是合法 JSON，系统没有执行任何记忆操作。"
    "请改用 memory_apply 工具重新提交（参数 ops 为操作数组，字符串里的换行写成 \\n）；"
    "如果不需要改，就直接说「没有改动」。")
_MSG_EMPTY_FINAL = (
    "（系统）你刚才只输出了思考过程，没有给出最终回答，也没有输出 tool / memory 块。"
    "现在不要再思考，直接输出最终回答；需要操作就立即输出对应的 ```tool 或 ```memory 块。")
# 输出被截断但工具块还没开始写：以前这种情况完全没有兜底，
# 正文停在「现在一次读清…：」就直接收尾，用户看到的就是「说到一半没了」。
_MSG_OUTPUT_TRUNCATED = (
    "（系统）上一轮因为输出长度上限被截断了：正文可能停在半句，工具块可能根本没写出来。"
    "不要从头重写，接着刚才的位置补完——"
    "该调工具就只发一个合法的 ```tool 块（参数尽量精简，例如 read_file 只读需要的行范围）；"
    "只是正文没说完，就把剩下的说完。")

# 正文以「决策问句」收尾却没调 ask_user：卡片能原地等回答，正文问句只能等用户再打一遍。
# 判定逻辑统一在 stepctx._pending_decision（自动续跑与提问提醒共用一份）。
_MSG_ASK_CARD = (
    "（系统）你刚才在正文里用问句问用户，但**没有调用 ask_user**。"
    "需要用户拍板的事必须走提问卡：调用 ask_user（questions 数组，每题可带 options；"
    "允许多选时加 multiple:true，题目会自动标注（多选）），"
    "由前端弹出卡片原地等待回答；不要再让用户自己在聊天框里回一遍。"
    "如果用户刚才是反问而不是回答，先正面回答，再重新用卡片确认原问题，不要自行假设。"
    "如果这事不需要用户决定，就直接自己做，不要问。")

# 思考里决定「先问用户」却直接开工：第一轮就把工具按回去，先要提问卡。
# 只在第一轮触发一次，避免模型每次思考都提到「问用户」时反复打断。
_MSG_ASK_BEFORE_ACT = (
    "（系统）你在思考里决定先向用户确认（风格 / 方向 / 偏好等），但这一轮没有调用 ask_user。"
    "如果这个选择会决定交付结果，请**先**调用 ask_user，把 2–3 个可选项摆出来再动手；"
    "不要按默认方案继续做。若其实不需要用户拍板，请明确说明你采用的默认值，然后再继续。")

_ASK_INTENT_RE = re.compile(
    r"(问用户|询问用户|向用户(?:确认|提问|询问)|和用户(?:确认|对齐)|"
    r"跟用户(?:确认|对齐)|让用户(?:选|确认|决定)|征求用户|"
    r"ask\s+the\s+user|ask\s+user|check\s+with\s+the\s+user|confirm\s+with\s+the\s+user)",
    re.I)
_ASK_NEG_RE = re.compile(
    r"(不需要|无需|不用|不必|没必要).{0,8}(问|确认|询问|征求)|"
    r"(no\s+need\s+to\s+ask|without\s+asking)", re.I)
_ASK_DECISION_RE = re.compile(
    r"(风格|样式|视觉|配色|设计|方向|方案|偏好|口味|需求|范围|格式|"
    r"style|design|direction|preference|format)", re.I)


def _reasoning_wants_ask(reasoning):
    """思考里是否明确打算「先问用户一个会影响交付的选择」。"""
    t = str(reasoning or "")
    if not t or len(t) > 8000:
        return False
    if _ASK_NEG_RE.search(t):
        return False
    return bool(_ASK_INTENT_RE.search(t) and _ASK_DECISION_RE.search(t))


def _needs_reasoning_ask_nudge(reasoning, tool_calls, nudged, rounds):
    """第一轮：思考里说要问用户，却没调 ask_user 就开始执行工具 → 拦一次。"""
    if nudged or rounds != 0:
        return False
    calls = tool_calls or []
    if not calls:
        return False
    if any((c.get("tool") or "") == "ask_user" for c in calls):
        return False
    return _reasoning_wants_ask(reasoning)

# 伪工具调用：模型把调用写成了纯文本（<run action=…> / DSML），运行时不会执行。
# 只做「点明 + 要求改用原生调用」，每回合最多提醒 2 次，不做额外刹车。
_MSG_PSEUDO_CALL = (
    "（系统）你刚才把**工具调用写成了纯文本**（例如 `<run action=\"exec\">…</run>`、"
    "`<read action=\"file\">…</read>` 或 DeepSeek 的 DSML 标签）。"
    "这些内容只是正文，**不会被当作工具执行**，所以你拿不到任何结果。"
    "请改用原生工具调用（function calling / tools）重新发起；不要在正文里描述调用，"
    "也不要自创 XML / DSML 写法。如果当前确实没有可用工具，直接说明缺哪个工具，"
    "不要假装已经执行，也不要把结论写成「需要用户手动执行」。")

# 用户对提问卡的反问：回答完必须把原问题重新摆出来，不能当成默认同意。
_MSG_ASK_BACK = (
    "（系统）用户刚才是在**反问**，不是回答你之前的问题。你已经回答了他的问题，"
    "但原问题仍然没有确定。现在必须调用 ask_user 重新确认原问题（列出选项），"
    "不要自行选默认值，也不要在确认前执行写文件 / 命令等有副作用的操作。")

# 执行前的计划闸门：需要动手的任务必须先出计划卡，否则用户看不到「要做什么」。
_MSG_PLAN_FIRST = (
    "（系统）这次需要动手（改文件 / 跑命令 / 多步处理），但还没有执行计划。"
    "请先调用 update_plan：简单任务给 1–8 步；大型任务给 2–6 个里程碑"
    "（每个 2–6 步，每步写清「目标 + verify」）。第一件要做的事标成 doing、"
    "其余 pending；然后再继续调用其他工具。"
    "确实只有一步的小改动，也先建成单步计划再动手。")

# 模型把「（调用工具）」占位文字当成正文时，明确要求它二选一。
_MSG_PSEUDO_FINAL = (
    "（系统）你上一轮只输出了「（调用工具）」这类占位文字，并没有真正发起工具调用。"
    "现在二选一：立刻发起需要的工具调用；如果已经做完，就直接给出结论。"
    "不要重复输出占位文字。")

# 占位文本的几种写法（全角 / 半角 / 裸写）
_PSEUDO_FINAL_WORDS = ("（调用工具）", "(调用工具)", "调用工具")


def _is_pseudo_final(text):
    """正文只有「（调用工具）」这类占位文字 → 不是回答（零产出）。

    多轮原生工具之后，模型会把历史里的占位符抄进正文；它既没有信息量，
    也不代表真的发起了调用，必须按空回合处理。
    """
    t = str(text or "").strip()
    if not t or len(t) > 80:
        return False
    if not any(w in t for w in _PSEUDO_FINAL_WORDS):
        return False
    rest = t
    for w in _PSEUDO_FINAL_WORDS:
        rest = rest.replace(w, "")
    rest = rest.strip(" \t\r\n。.!！?？…、,，")
    return not rest


def _inject_prev_end_note(messages, end_reason):
    """把上一轮的真实结束原因告诉模型，避免它自己编「工具没有返回结果」。

    插在最后一条 user 消息之前：模型看得到，又不改变本轮真正的提问顺序。
    """
    text = str((end_reason or {}).get("text") or "").strip()
    if not text or not messages:
        return messages
    note = ("（系统）上一轮以「%s」结束，不是正常完成。不要猜测中断原因，"
            "也不要声称「工具没有返回结果」；先核对当前文件 / 会话状态再继续。" % text)
    idx = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            idx = i
            break
    messages.insert(idx, {"role": "user", "content": note})
    return messages


# 伪工具标签的识别 / 清理统一走 llm（白名单规则），这里不再各维护一份正则。
# 「已创建 / 已加入 / 已登记…」这类完成声明，用于检查是否只在说空话。
_DONE_CLAIM_RE = re.compile(
    r"已(?:经)?(?:创建|添加|加入|登记|写入|保存|更新|删除|设置|安排|记录|存入|落库|记入|入|放入|放到|归档|建好|设好|写好|写完|完成|改短|改好|改成|修改|调整|精简|压缩|合并|补上|补进|记下|删掉|运行|执行|跑完|跑通|定位|找到|查到|搜到)"
    r"|(?:建好|设好|写好|写完|存好|改短|改好|改完|删掉|记下|补上|跑完|跑通)了"
    r"|现在(?:运行|执行|开始运行|开始执行)")
# 记忆修改声明：用于触发一次「补做」回合（不是所有完成声明都能安全重试）。
_MEMORY_CLAIM_RE = re.compile(
    r"已(?:经)?(?:改短|改好|改成|修改|更新|调整|精简|压缩|合并|删除|删掉|记下|记录|补上|补进)"
    r"|(?:改短|改好|改完|删掉|记下|补上|更新)了"
    r"|现在(?:真的)?改|马上改|这就改|真的改")
# 「工具已发出 / 正在查 / 等结果」这类「进行中」声明：没有工具调用就是空话。
_PENDING_CLAIM_RE = re.compile(
    r"(?:工具|请求|调用|查询).{0,8}(?:已|已经)?(?:发出|发送|提交|执行)"
    r"|(?:正在|马上|这就|立刻|我).{0,6}(?:查|查询|读取|拉取|搜索|获取|调用)"
    r"|(?:等|等待).{0,10}(?:结果|返回|工具)"
    r"|(?:我|先|来|去|马上|这就|立刻).{0,4}"
    r"(?:看一下|看看|读一下|读读|写一下|查一下|搜一下|建一下|建(?!议)|"
    r"加一下|加(?!油)|设置一下|设置|安排一下|安排|创建|修改|检查|分析)"
)

# 非 Agent 会话只剩「查询类」能力（联网 / 抓取）：那里只该核对搜索类声明。
# 通用 _PENDING_CLAIM_RE 里的「修改 / 检查 / 分析 / 创建」在讲课、闲聊里到处都是
# （现场：「先把一行加上去」被判成「说了要调用工具」），必须按模式分开。
_PENDING_CLAIM_SEARCH_RE = re.compile(
    r"(?:工具|请求|调用|查询).{0,8}(?:已|已经)?(?:发出|发送|提交|执行)"
    r"|(?:正在|马上|这就|立刻|我).{0,6}(?:查一下|查查|查询|搜索|搜一下|搜搜|检索|拉取|抓取)"
    r"|(?:等|等待).{0,10}(?:结果|返回|工具)")

_MSG_TOOL_CLAIM_MISSING = (
    "（系统）你刚才说「工具已发出 / 正在查 / 等结果」，但本轮**没有任何工具调用被执行**。"
    "请立刻在回复末尾输出合法的 ```tool 代码块（不要裸 JSON），"
    "或如实说明你无法完成；不要只说「已发出 / 等一下」。")

_MSG_INTENT_MISSING = (
    "（系统核对）用户本轮明确要求「%s」，但你还没有调用任何工具。"
    "请立刻在回复末尾输出合法的 ```tool 代码块，参数优先使用：%s；"
    "如果缺少必需参数，请用 ask_user 问清楚，不要只给文字说明。")

# 原生 function calling 可用时，以上提醒一律换成「直接发起原生调用」的说法：
# 收尾提醒里只要还教 ```tool，模型就会被推回文本通道 —— 那正是 leak 的来源。
_MSG_BAD_TOOL_NATIVE = (
    "（系统）你刚才把工具调用写成了正文里的文本，系统已清理、**没有执行**。"
    "需要工具就直接通过原生工具调用发起；或者不再调用工具，直接用文字回答。")
_MSG_OUTPUT_TRUNCATED_NATIVE = (
    "（系统）上一轮因为输出长度上限被截断了：正文可能停在半句，调用也可能没发出来。"
    "不要从头重写，接着刚才的位置补完——该调工具就直接发原生调用（参数尽量精简）；"
    "只是正文没说完，就把剩下的说完。")
_MSG_TOOL_CLAIM_MISSING_NATIVE = (
    "（系统）你刚才说「工具已发出 / 正在查 / 等结果」，但本轮**没有任何工具调用被执行**。"
    "请立刻直接发起原生工具调用，或如实说明你无法完成；不要只说「已发出 / 等一下」。")
_MSG_INTENT_MISSING_NATIVE = (
    "（系统核对）用户本轮明确要求「%s」，但你还没有调用任何工具。"
    "请立刻直接发起原生工具调用，参数优先使用：%s；"
    "如果缺少必需参数，请用 ask_user 问清楚，不要只给文字说明。")
_MSG_EMPTY_FINAL_NATIVE = (
    "（系统）你刚才只输出了思考过程，没有给出最终回答，也没有调用工具。"
    "现在不要再思考，直接输出最终回答；需要操作就直接发起原生工具调用。")


def _nudge_msg(native, native_text, text_msg):
    """按本轮通道选收尾提醒：原生可用时绝不提 ```tool。"""
    return native_text if native else text_msg



def _needs_intent_nudge(text, hint, has_tools, nudged):
    """高置信度意图 + 模型完全没调工具 → 给它一次回到工具轨道的机会。

    只对 0.9 以上的明确指令生效；模型已经在提问 / 说明无法完成时不打扰。
    """
    if not hint or nudged or has_tools:
        return False
    try:
        if float(hint.get("confidence") or 0) < 0.9:
            return False
    except (TypeError, ValueError):
        return False
    t = str(text or "").strip()
    if not t:
        return False
    if re.search(r"[?？]|请问|需要你|缺少|无法|不能|没找到|找不到", t):
        return False
    return True


def _clean_assistant_text(text):
    """去掉伪工具标签与残留的 <memory> 控制块，并保证标题前有换行。

    用的是 llm 的白名单规则（未知 ASCII 标签一律当噪音），不再枚举 <tool...> 写法。
    """
    text = llm._strip_unknown_tags(text or "")
    text = mem_tool.strip_memory_tags(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _claims_done(text):
    return bool(_DONE_CLAIM_RE.search(text or ""))


def _tool_failed(t):
    """工具结果以 ❌ 开头表示失败（agent.run_tool 的约定）。"""
    return str((t or {}).get("result") or "").lstrip().startswith("❌")


def _done_claim_verdict(text, tool_log, mem_ops_all, agent_mode=True):
    """检查「已…」完成声明是否成立，返回警告文案（空串 = 成立，不警告）。

    只有两种「不成立」：本轮**一个工具都没成功**，或**写操作全部失败**。
    以前是「只要有一个工具失败就算不成立」，于是一轮里 40 个工具成功、
    1 个 run_command 参数写坏，也会被判定成「没有成功执行对应工具」，
    与实际正好相反（见 NOTE 43.86）。

    agent_mode=False：本会话没有文件 / 命令能力，唯一能落地的只有知识库写入，
    所以只核对「声称改记忆却没有 mem ops」，不再拿工具清单说事
    （讲课时「写完了」会被当成完成声明，属于误判）。
    """
    if not (text or "").strip() or not _claims_done(text):
        return ""
    if not agent_mode:
        if mem_ops_all or not _claims_memory(text):
            return ""
        return "⚠ 系统核对：本轮没有写入知识库，以上「已改」的声明不成立。"
    ok_tools = [t for t in tool_log if not _tool_failed(t)]
    if not ok_tools and not mem_ops_all:
        return ("⚠ 系统核对：本轮没有成功执行任何工具，以上「已…」的声明只是文字描述，"
                "并未真正执行。")
    ok_writes = [t for t in tool_log if t.get("write") and not _tool_failed(t)]
    failed_writes = [t for t in tool_log if t.get("write") and _tool_failed(t)]
    if failed_writes and not ok_writes:
        return "⚠ 系统核对：本轮写操作全部失败，以上完成声明不成立。"
    return ""


def _claims_memory(text):
    s = str(text or "")
    if not _MEMORY_CLAIM_RE.search(s):
        return False
    # 「现在改 / 改短 / 改好」本身就指向记忆整理；
    # 「已更新 / 已删除」这类太泛，要求上下文出现记忆相关词。
    if re.search(r"现在(?:真的)?改|马上改|这就改|真的改|改短|改好", s):
        return True
    return any(k in s for k in ("记忆", "条目", "节点"))


def _claims_pending_tool(text, agent_mode=True):
    """「工具已发出 / 正在查」这类声明。

    agent_mode=False（没有文件 / 命令能力）时只认搜索类说法：
    讲课时「我先把这一行加上去」不该被当成「说了要调用工具」。
    """
    pat = _PENDING_CLAIM_RE if agent_mode else _PENDING_CLAIM_SEARCH_RE
    return bool(pat.search(text or ""))


def _needs_tool_claim_nudge(text, has_tools, nudged, agent_mode=True):
    """说「工具已发出 / 等结果」却没有工具调用 → 给它一次真的调用的机会。"""
    return bool(text and _claims_pending_tool(text, agent_mode)
                and not has_tools and not nudged)


def _needs_memory_nudge(text, mem_ops, nudged):
    """声称改了记忆却没有 memory ops → 给它一次补做的机会（只补一次）。"""
    return bool(text and _claims_memory(text) and not mem_ops and not nudged)


def _needs_memory_format_nudge(mem_block, mem_closed, mem_ops, nudged):
    """记忆块已闭合但 JSON 解析不出 ops → 提醒重发合法 JSON（只补一次）。"""
    return bool(mem_block and mem_closed and not mem_ops and not nudged)


def _needs_empty_final_nudge(visible, reasoning, has_actions, has_control, nudged):
    """只输出了思考、没有正文/工具/控制块 → 关掉思考再要一次最终回答。"""
    return bool(not (visible or "").strip() and (reasoning or "").strip()
                and not has_actions and not has_control and not nudged)


def _needs_ask_nudge(text, has_ask, nudged):
    """正文以决策问句收尾、本轮又没调 ask_user → 提醒它改用提问卡（只提醒一次）。

    判定与自动续跑共用 stepctx._pending_decision：结尾必须是「等你拍板」的问句，
    且不涉及继续 / 下一步。解说里的反问、以及本该继续做的话都不该变成卡片。
    """
    if nudged or has_ask:
        return False
    return stepctx._pending_decision(text)


# 用户在提问卡里反问（而不是选择）的识别。只在「答案本身像问题」时提示模型：
# 这类回复不是授权，不能当成默认同意继续执行。
_QUESTION_RE = re.compile(
    r"(怎么|如何|为什么|为啥|是什么|有什么|什么(?:意思|区别|样|时候)|哪个|哪些|"
    r"是否|能不能|可不可以|要不要|行不行|好不好|吗[？?]?$|呢[？?]?$)")

# 反问里已经带了明确动作（「按那个方式改一下」）→ 视为授权，不再强制重新确认。
_AUTHORIZE_RE = re.compile(
    r"(按这个|按那个|按这种|按那种|照这个|照那个|就按|直接|就这么)"
    r".{0,10}(改|做|实现|执行|写|加|删|替换|动手)|接着改")


def _looks_like_question(text):
    """这条回复是不是在反问，而不是给答案 / 做选择。"""
    t = str(text or "").strip()
    if not t or t == "（未回答）":
        return False
    if re.search(r"[?？]\s*$", t):
        return True
    return bool(_QUESTION_RE.search(t))


def _adopt_bare_calls(rr):
    """把可见文本末尾的裸 {"calls":[...]} 提升为真正的工具调用。

    有些模型不写 ```tool 围栏，直接吐 JSON；旧进程/旧路径可能漏掉 llm 侧的兜底。
    这里再兜一层：提升成功返回 True，调用方应把前端已显示的裸 JSON 用 replace 清掉。
    """
    if not isinstance(rr, dict) or rr.get("tool_calls") or not rr.get("visible"):
        return False
    before, bare = llm.extract_bare_calls(rr.get("visible") or "")
    calls = agent.parse_calls(bare) if bare else []
    if not calls:
        # 没有可执行调用，但可能清掉了标签 / 坏掉的调用块：正文照样要更新
        if before != rr.get("visible"):
            rr["visible"] = before
        return False
    rr["visible"] = before
    rr["tool_block"] = bare
    rr["tool_calls"] = calls
    return True


def _strip_final_bare_calls(text):
    """最终正文里如果还残留裸工具 JSON，就摘掉；返回 (正文, 是否发现)。"""
    before, bare = llm.extract_bare_calls(text or "")
    if not bare:
        return before, False
    return before.strip(), True


# 能力工具的域关键词：只有命中某个域，才把该域的 schema 挂给模型（agent_cap_tools=auto）。
# 股市 / 日程 / 记忆 / 学习这几个域的工具在普通对话里几乎不用，
# 却每轮要占近 1k token 的 schema 预算，还会稀释模型对文件工具的注意力。
_CAP_DOMAIN_KEYS = {
    "calendar": ("日程", "会议", "安排", "提醒", "闹钟", "每周", "每天", "几点", "预约"),
    "stock": ("股市", "股票", "持仓", "k线", "行情", "收益", "模拟盘", "标的", "个股"),
    "memory": ("记忆", "记住", "回忆", "忘记", "知识库", "记一下", "帮我记"),
    "observe": ("学习", "复盘", "错题", "练习", "刷题"),
}


def _cap_domains_for(text, intent=None):
    """按本轮用户消息 + 意图提示决定挂哪些能力域；None 表示「全挂」。

    能力域同时决定「工具组」是否下发 schema（见 agent.TOOL_GROUPS）：
    低频工具不该每轮都占预算，但命中关键词时必须整组出现，否则模型会以为没有。
    """
    low = str(text or "").lower()
    doms = set()
    for dom, keys in _CAP_DOMAIN_KEYS.items():
        if any(k in low for k in keys):
            doms.add(dom)
    for dom, keys in agent.TOOL_GROUP_KEYS.items():
        if any(k in low for k in keys):
            doms.add(dom)
    tool = str((intent or {}).get("tool") or "")
    if "." in tool:
        doms.add(tool.split(".")[0])
    return doms


def _cap_domains_with_skills(doms, has_skills):
    """Skill 索引注入时，保证 skill_read 也在本轮 schema 里。

    索引每轮都在，但能力域是惰性挂载的；不补这一项，模型会看到
    「调用 skill_read」却找不到这个工具（实测全库调用数为 0）。
    """
    if doms is not None and has_skills:
        doms.add("skill")
    return doms
