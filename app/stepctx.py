# -*- coding: utf-8 -*-
"""多轮工具循环里的「步骤上下文」。

两个纯函数，只产出给模型看的文字，不碰状态：

- summarize_reasoning：把上一轮的思考压成一小段，随工具结果一起回灌。
  DeepSeek 一类接口不允许把 reasoning_content 原样放回 messages，
  所以这里走「文字摘要」路线：既保住上一轮的判断，又不污染协议。
- fail_diagnose：工具失败时补一条分类提示（参数错 / 前提错 / 方法错），
  避免模型拿着同样的参数反复重试。
- budget_hint：轮次过半时提醒收敛，先给中间结论。
"""

import re

_KEYWORDS = ("下一步", "接下来", "所以", "因为", "需要先", "应该", "决定", "结论")

# 按工具 + 错误特征给一条「最有用的下一步」，不做通用说教
_HINTS = (
    ("edit_file", ("未找到", "找不到", "不是唯一", "不唯一", "必须唯一"),
     "old 片段与文件当前内容对不上：先 read_file 看清原文（缩进、空行也要一致），"
     "或改用 write_file 整篇写入。"),
    ("read_file", ("不存在", "找不到", "No such file"),
     "路径不存在：先 list_dir 确认目录结构与文件名大小写。"),
    ("list_dir", ("不存在", "找不到", "No such file"),
     "目录不存在：先确认沙箱根目录，再逐层往下看。"),
    ("run_command", ("超时", "timeout", "拒绝", "不允许", "白名单"),
     "命令没有正常结束：拆成更小的命令、只跑单个测试；"
     "被白名单拒绝时如实说明，不要换写法绕过。"),
    ("write_file", ("语法", "SyntaxError", "自检"),
     "写入后自检失败：按报错里的第一处行号修正，不要整篇重写。"),
)

_GENERIC = "先判断是哪一类问题：参数写错 / 前提假设不成立（文件已变、目录不存在）/ 方法选错。"


def _sentences(text):
    """按中英文句末标点与换行切句；不切英文句点（小数点和文件名会误伤）。"""
    t = (text or "").replace("\r\n", "\n").strip()
    if not t:
        return []
    out, buf = [], ""
    for ch in t:
        buf += ch
        if ch in "。！？!?\n":
            s = buf.strip()
            if s:
                out.append(s)
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def summarize_reasoning(text, limit=240):
    """取「最后两句 + 含结论词/下一步的句子」，按原顺序拼起来，超长保尾部。

    保尾部是因为推理的结论通常落在末尾；超长时在开头加省略号，明确这是节选。
    """
    ss = _sentences(text)
    if not ss:
        return ""
    tail_start = max(0, len(ss) - 2)
    keep = set(range(tail_start, len(ss)))
    for i in range(max(0, len(ss) - 6), len(ss)):
        if any(k in ss[i] for k in _KEYWORDS):
            keep.add(i)
    picked = "".join(ss[i] for i in sorted(keep)).strip()
    if limit and len(picked) > limit:
        picked = "…" + picked[-limit:].lstrip("，。； ")
    return picked


def fail_diagnose(name, err_text):
    """工具失败时追加的一段诊断；匹配不到就给通用分类法。"""
    err = str(err_text or "")
    for tool, keys, hint in _HINTS:
        if tool != name:
            continue
        if any(k in err for k in keys):
            return "【失败诊断】%s\n不要用完全相同的参数重试。" % hint
    return "【失败诊断】%s\n不要用完全相同的参数重试。" % _GENERIC


def join_turns(parts):
    """把多轮正文按「一轮一段」拼起来：轮与轮之间空一行。

    直接 join 会让「第二步的结论」粘在「第一步的说明」后面，
    读起来像一句话；分段后用户才能看出哪句是阶段结论、哪句是最终结论。
    """
    segs = [str(p).strip() for p in (parts or []) if str(p or "").strip()]
    return "\n\n".join(segs)


def budget_hint(rounds, max_rounds):
    """只在「还剩 3 轮」时提醒一次收尾；其它轮次返回空串。

    以前在 1/3、2/3 轮就劝停，等于把「一次只推进一步」写进了系统里；
    24 轮本就够用，只在快用完时才需要提醒。
    """
    try:
        rounds = int(rounds)
        max_rounds = int(max_rounds)
    except (TypeError, ValueError):
        return ""
    if max_rounds <= 3 or rounds <= 0:
        return ""       # 0 = 不限（设置里可以填 0），也就没有「还剩 3 轮」这回事
    if max_rounds - rounds != 3:
        return ""
    return ("【预算】还剩 3 轮工具调用。优先把当前这一步收尾；"
            "没做完的部分明确告诉用户还剩什么，不要硬凑。")


PROGRESS_EVERY = 20        # 每多少轮做一次「进度自查」


def progress_hint(rounds):
    """每 20 轮注入一次「先自查、再决定要不要继续」的软提示。

    为什么需要：`max_rounds = 0`（不限）时 `budget_hint` 永远返回空串，模型
    完全收不到「已经跑很久了」的信号，容易在同一类做法里反复打转、不断扩范围。
    这里只是提醒它自查一次，不改变「0 = 不限」的语义，也不替它刹车。
    """
    try:
        rounds = int(rounds)
    except (TypeError, ValueError):
        return ""
    if rounds <= 0 or rounds % PROGRESS_EVERY != 0:
        return ""
    return ("【进度检查】这已经是第 %d 轮工具循环。先用一两句话说明："
            "已经完成了什么、还剩什么；如果已经满足用户要求，就直接收尾并给结论，"
            "不要为了「更完整」继续扩大范围。" % rounds)


def plan_health_hint(plan, rounds_since_update=0, recent_failures=0,
                     milestone_done="", plan_stale=0):
    """计划体检：发现/失败/里程碑完成/长期没更新时，要求用 plan_patch 改计划。

    只给**具体动作**，不说「请更新计划」这种泛化提醒：
    - 某里程碑完成 → 把下一个里程碑展开成可执行步骤；
    - 最近失败 ≥ 2 → 拆步或标 blocked；
    - 计划连续 N 轮没变 → 有新结论就 patch，没有就明确说计划仍成立；
    - 已经 6 轮没更新 → 同上（长任务里防止计划与实际脱节）。
    """
    steps = (plan or {}).get("steps") or []
    if not steps:
        return ""
    try:
        rounds_since_update = int(rounds_since_update)
        recent_failures = int(recent_failures)
        plan_stale = int(plan_stale)
    except (TypeError, ValueError):
        return ""
    if milestone_done:
        return ("【计划体检】里程碑「%s」已经完成。用 plan_patch 把下一个里程碑"
                "展开成 2–6 个可执行步骤（每步带 verify），并把刚完成这一步的经验"
                "记进 log（kind=decision）；不要重写整份计划。" % milestone_done)
    if recent_failures >= 2:
        return ("【计划体检】最近 %d 个工具失败。先用 plan_patch 处理："
                "把失败那步拆小、换验证方式，或标 blocked 并写清缺什么；"
                "不要用同样的参数再试一次。" % recent_failures)
    if plan_stale >= 1:
        return ("【计划体检】计划已经连续 %d 轮没有变化。如果这轮拿到了新事实"
                "（报错、约束、接口不符），用 plan_patch 记录 discovery 并改受影响步骤；"
                "如果计划仍然成立，就在正文里明确说「计划不变，因为…」，不要默默略过。"
                % plan_stale)
    if rounds_since_update >= 6:
        return ("【计划体检】已经 %d 轮没有更新计划。检查一下：里程碑是否该推进 / "
                "步骤是否还准确；有新结论就 plan_patch，没有就继续。" % rounds_since_update)
    return ""


def memory_organize_hint(rounds, nodes, edges, soft_limit=60, every=20):
    """每 N 个工具轮提醒一次整理本会话记忆。

    触发条件：到了 N 的倍数，并且（条目超过软上限，或一条关系都没有但已有
    20 条以上）。后者用来给长期 0 关系的会话「起个头」。
    """
    try:
        rounds = int(rounds)
        nodes = int(nodes)
        edges = int(edges)
        soft_limit = int(soft_limit)
        every = int(every)
    except (TypeError, ValueError):
        return ""
    if every <= 0 or rounds <= 0 or rounds % every != 0:
        return ""
    over = soft_limit > 0 and nodes >= soft_limit
    bootstrap = edges == 0 and nodes > 20
    if not (over or bootstrap):
        return ""
    return ("【记忆整理】本会话记忆已有 %d 条、%d 条关系。"
            "若本轮新增了事件 / 进展，先合并同主题重复（保留信息最全的一条，"
            "update 合并正文与别名、其余 delete），删除过时条目，"
            "并给关键事件补一条 purpose / sequence / cause / condition 关系；"
            "整理完再继续当前任务。" % (nodes, edges))


# 结尾只在征求「要不要我继续」：这类确实该继续做。
# 不能把「要不要按方案 A 做」也算进来 —— 那是等用户拍板，自动续跑会逼它自己决定。
_CONTINUE_ASK = ("要不要我继续", "要不要继续", "需要我继续", "是否要我继续",
                 "我可以继续", "要继续", "是否继续", "需要继续", "要我继续",
                 "继续吗", "接着做吗")

# 结尾在等用户拍板的决策问句（不是「继续」，而是选方案 / 确认做法）
_DECISION_ASK_RE = re.compile(
    r"要不要|是否需要|是否|需要我|要我|你想|你希望|你倾向|选哪|哪个|哪种|哪一套|"
    r"可以吗|行吗|确认一下")
_DECISION_CONTINUE_HINT = ("继续", "下一步", "接着")

# 「需要你决定」类清单：最后一句常常不是问句（「你直接说按你的来，我就开写」），
# 但整段确实是在等用户拍板。只看最后一行会漏掉，必须看最后一段。
_DECISION_SECTION_RE = re.compile(
    r"需要你(?:决定|确认|拍板|选择)|等你(?:确认|决定|拍板|选择)|"
    r"请你(?:决定|确认|拍板|选择)|你来(?:决定|定|选|拍板)|"
    r"由你(?:决定|定|选)|按你的来|选一个|三选一|二选一")
_HANDOFF_RE = re.compile(r"你直接说|按你的来|你来定|你拍板|等你(?:确认|决定|拍板)")
# 有明确选项的清单：编号项 / 备选 / 方案 A
_DECISION_CHOICE_RE = re.compile(
    r"(?:^|\n)\s*\d+[.、]\s|备选|选项|方案\s*[A-Za-z1-9]", re.M)

# 明确表示「已经做完」的话术：用于放行，避免把正常完成也当成提前收尾
_COMPLETE = ("已完成", "全部完成", "任务完成", "已经完成", "改完了", "都改好了",
             "全部改好", "已经改好", "搞定", "完成了")

# 开发型任务的意图词：只有这类才在「没计划、也没动手」时催一次
_DEV_GOAL = ("实现", "开发", "重构", "迁移", "改造", "新增", "修复", "做一个",
             "搞一个", "写一个", "完成整个", "补齐", "加上", "接入")

# 正文结尾承诺了下一步动作：开发任务里最常见的「说而不做」
# （「先读那段确认原文再改」「现在把 X 加进去」「接下来处理 Y」）
# 词表只留「第一人称 + 动作」的强信号：「补上 / 看一下 / 确认一下 / 接下来」
# 在正常收尾句里太常见（「你确认一下效果」「随时补上」「接下来你可以自己试」），
# 会把交付说明误判成「还要继续」。带「接下来」的动作交给下面的正则判定。
_PROMISE_WORK = ("先读", "先看", "先改", "先查", "先确认", "先补", "先加", "先跑",
                 "现在去", "现在把", "现在开始", "现在改", "现在加", "现在读", "现在补",
                 "下一步", "这一批", "同时确认", "同时改", "再读", "再改", "再补",
                 "读清", "读一下", "接上", "再接")

# 词表之外的「换一种说法」：43.76 补词表后，模型又用「现在重写一版 / 然后重新测量」
# 绕过去了，所以再加一层模式匹配。锚定在句首的动作引导词上，避免命中正文里的普通叙述。
_ACTION_RE = re.compile(
    r"(?:现在|接下来|下一步|然后|接着|下面|随后).{0,30}"
    r"(?:重写|重做|重测|重建|重新|再试|再跑|再测|再算|改成|换成|换掉|跑一遍|测一遍"
    r"|处理|修改|改掉|读取|写入|补|加上|删掉|执行|动手|验证|看一下|看看)")

# 「我会插入 ORE_DROPS 定义」这类将来时承诺：同样是说而不做，但不符合句首引导词。
_FUTURE_ACTION_RE = re.compile(
    r"(?:我会|我将|我这就|马上|随后)(?:先|再|把|去|开始)?"
    r".{0,12}(?:插入|写入|修改|改掉|加上|加入|补上|补进|删掉|重写|替换|运行|验证|读取|查看)")

# 句首直接一个动作 + 对象（「查 applyTech 的调用点。」「读 foo.js。」）。
# 现场 s_2809 idx781：模型连续几轮只写这种短句就停，旧的承诺词表只认
# 「先查 / 现在执行」，于是既没续跑、也没留下任何中断说明。
# 现场 s_2809 idx828：同类漏判又出现了两次 ——「运行扫描脚本，取得旧数字」
# 和「扫描文档里实际存在的规模数字。」，所以把「运行 / 扫描 / 执行 / 统计 /
# 同步 / 对比 / 更新 / 搜索」这类同义动词一并收进来。
# 只认句首动词，避免把「你可以查看文档」这种交付说明算成承诺。
_BARE_ACTION_RE = re.compile(
    r"(?:^|[。；\n])\s*(?:查|读|改|写|跑|验证|确认|检查|修复|重写|替换|删"
    r"|扫描|运行|执行|统计|同步|对比|更新|搜索)"
    r"[^。；\n]{0,50}(?:。|；)?\s*$")

# 用户这轮在问「为什么 / 进度 / 是否完成」：不是让模型继续执行。
# 反例：「为什么失败？帮我修一下」带明确任务动词 → 不算提问轮。
_META_TURN_RE = re.compile(
    r"为什么|为啥|怎么回事|什么原因|咋回事|"
    r"(?:刚才|上次|上一?轮|这一?轮|这次).{0,12}(?:停|断|中断|卡|失败|结束)|"
    r"(?:现在|目前|当前|到底).{0,12}(?:做到|到哪|进度|状态|完成)|"
    r"总结一下|汇总一下|复盘|解释一下|说明一下|分析一下|进度如何|做到哪|"
    r"完成了?[吗嘛?？]|做完了?[吗嘛?？]|完美完成|全部完成[了吗嘛?？]")
_TASK_CONTINUE_RE = re.compile(
    r"继续(?:做|改|写|跑|执行|任务|开发|实现)|接着(?:做|改|写|跑)|"
    r"开始(?:做|改|写|实现|开发)|去(?:做|改|写|实现|跑)|"
    r"帮我(?:做|改|写|实现|修|加|删)|把.{0,20}(?:改|修|加|删|实现)")

# 阶段总结的信号：成段正文里出现这些词，说明它在汇报「已经发生了什么」，
# 不是一句「继续做 X」的占位承诺。
_SUMMARY_MARK = ("已完成", "已通过", "已修复", "已改完", "已生成", "已跑",
                 "结果", "目前", "现在已经", "实测", "验证通过", "PASS",
                 "0 FAIL", "全部通过", "已经改", "已经修")


def is_meta_turn(text):
    """这一轮用户是不是在问「为什么 / 进度 / 是否完成」，而不是让模型继续做。

    命中时调用方应关闭自动续跑与计划体检，允许模型用一段文字作答后收尾。
    """
    t = str(text or "").strip()
    if not t or len(t) > 240:
        return False
    if _TASK_CONTINUE_RE.search(t):
        return False
    return bool(_META_TURN_RE.search(t))


def is_checkpoint_summary(text, chars=180):
    """成段的阶段总结：够长 + 带进度/结论信号，允许先收尾再等用户决定。

    一句「继续。S1：核对…」既不够长、也没有总结信号，仍然会被自动续跑。
    `chars=0` 关闭这个放行。
    """
    t = str(text or "").strip()
    try:
        chars = int(chars)
    except (TypeError, ValueError):
        chars = 180
    if chars <= 0 or len(t) < chars:
        return False
    if t.endswith(("：", ":")):
        return False
    if _pending_decision(t) or _tail_asks(t):
        return False
    # 总结信号必须在**尾部**：一段长叙述开头写「已完成」、结尾却在承诺下一步时，
    # 仍然按「说要做」续跑；真正的阶段总结通常把结论放在最后。
    return any(k in t[-300:] for k in _SUMMARY_MARK)

# 明确「做不下去 / 等用户补材料」的话术：这不是「没做完」，催也做不出来。
# 现场：计划里挂着 1 步 pending，正文写「第 5 题的题面我始终没有拿到」，
# 自动续跑 3 轮都没有工具可调，最后撞上空转刹车 —— 提示就是这么反复出现的。
_BLOCKED_RE = re.compile(
    r"无法(?:继续|完成|写出|进行)|没法(?:继续|完成)|不能继续|做不下去"
    r"|(?:拿不到|没有拿到|无法获取|获取不到)(?:题面|题干|文件|内容|图片|数据|素材|参数)?"
    r"|缺少(?:题面|题干|材料|信息|参数|文件|数据|输入)"
    r"|需要你(?:提供|补充|确认|发|给)"
    r"|等你(?:提供|补充|发|给|确认)")

# 说了受阻、但紧接着表示「我自己接着做」：那还是能推进，不算等用户。
_SELF_CONTINUE = ("我会继续", "我继续", "我先继续", "先继续做", "我自己继续",
                  "我会先", "我先把", "接下来我")


def _pending_steps(plan):
    # blocked 表示「等用户决定」，不算未完成，否则续跑会卡在这一步上转圈
    steps = (plan or {}).get("steps") or []
    return [str(s.get("text") or "").strip()
            for s in steps if str(s.get("status") or "pending") not in ("done", "blocked")]


def _milestone_title(plan, mid):
    for m in ((plan or {}).get("milestones") or []):
        if isinstance(m, dict) and str(m.get("id") or "") == str(mid or ""):
            return str(m.get("title") or mid or "")
    return str(mid or "")


def ready_steps(plan, limit=3):
    """按 deps 计算「现在就能做」的步骤：doing 优先，其次依赖已完成的 pending。

    deps 里引用了不存在的 id 视为无依赖（模型经常漏写 / 写错 id，不能因此卡死）。
    pending 优先凑同一个里程碑（一轮做一个切片），避免一轮里跨里程碑来回跳。
    """
    steps = [s for s in ((plan or {}).get("steps") or []) if isinstance(s, dict)]
    by_id = {str(s.get("id") or ""): s for s in steps}
    done = {sid for sid, s in by_id.items()
            if str(s.get("status") or "") == "done"}

    def _deps_ok(s):
        for d in (s.get("deps") or []):
            key = str(d or "")
            if not key or key not in by_id:
                continue
            if key not in done:
                return False
        return True

    out = []
    seen = set()
    for s in steps:
        if str(s.get("status") or "") != "doing":
            continue
        sid = str(s.get("id") or "")
        if sid in seen:
            continue
        seen.add(sid)
        out.append(s)
    pending = []
    for s in steps:
        if str(s.get("status") or "") != "pending" or not _deps_ok(s):
            continue
        sid = str(s.get("id") or "")
        if sid in seen:
            continue
        seen.add(sid)
        pending.append(s)
    if out:
        anchor = str(out[0].get("milestone") or "")
    else:
        anchor = str(pending[0].get("milestone") or "") if pending else ""
    if anchor:
        # 一轮做一个切片：先把同一里程碑的步骤凑满，再考虑别的里程碑
        pending.sort(key=lambda s: 0 if str(s.get("milestone") or "") == anchor else 1)
    for s in pending:
        out.append(s)
        if len(out) >= max(1, int(limit or 3)):
            break
    return out


def plan_ready_hint(plan, limit=3):
    """给模型看的「本轮可执行」清单：具体到 milestone / step / verify / 可并行。"""
    ready = ready_steps(plan, limit)
    if not ready:
        return ""
    lines = ["【本轮可执行】"]
    for i, s in enumerate(ready):
        sid = str(s.get("id") or "")
        mid = str(s.get("milestone") or "")
        title = _milestone_title(plan, mid) if mid else ""
        head = ("%s %s" % (mid, sid)).strip()
        text = str(s.get("text") or "").strip()
        line = "- " + ("%s %s" % (head, text)).strip()
        if s.get("verify"):
            line += "（验证：%s）" % str(s.get("verify"))
        if str(s.get("status") or "") == "doing":
            line = "- 继续：" + line[2:]
        elif i > 0:
            line += "  ← 与上一步互不依赖，可同一轮并行"
        if title:
            line += "  [%s]" % title
        lines.append(line)
    if len(ready) > 1:
        mids = {str(s.get("milestone") or "") for s in ready}
        if len(mids) == 1 and "" not in mids:
            lines.append("→ 建议本轮把 %s 这 %d 步当一个切片一起做完，"
                         "完成后用 plan_patch 一次标状态，再进入下一个里程碑。"
                         % (mids.pop(), len(ready)))
        else:
            lines.append("→ 建议本轮把这些步骤一起推进，做完用 plan_patch 一次标状态，"
                         "不要一步一轮。")
    return "\n".join(lines)


def plan_retro_hint(plan):
    """整份计划做完、但还没写复盘时，催一次 Outcomes & Retrospective。

    Codex 的 ExecPlan 要求计划完成时写「结果 / 剩余 / 经验」；没有这一条，
    计划卡最后一格永远是空的，下一个接手的人（或下一轮模型）不知道结论。
    """
    steps = [s for s in ((plan or {}).get("steps") or []) if isinstance(s, dict)]
    if not steps:
        return ""
    if any(str(s.get("status") or "") != "done" for s in steps):
        return ""
    if str((plan or {}).get("retro") or "").strip():
        return ""
    return ("【收尾复盘】计划已全部完成，但还没有复盘。用 plan_patch 写 retro，3–6 行说清："
            "①实际交付了什么；②与计划的偏差 / 没做的部分；③可复用的经验。"
            "写完再给用户收尾结论，不要开新范围。")


def plan_replan_hint(plan_stale):
    """计划连续多轮没推进：按活文档要求强制重写，而不是只提醒。

    Codex 的规则是「计划随进展 / 发现 / 决策持续修订」；连续停滞说明旧的
    剩余步骤已经不成立，继续按它执行只会空转。
    """
    try:
        plan_stale = int(plan_stale)
    except (TypeError, ValueError):
        return ""
    if plan_stale < 2:
        return ""
    return ("【计划体检】计划已经连续 %d 轮没有推进。本轮**先调用 update_plan 提交修订版**"
            "（保留已完成步骤，重写剩余步骤 / 里程碑 / deps），再继续执行；"
            "只补 log、或只在正文里说「计划不变」，都不算推进。" % plan_stale)


# 正文里「宣称已经完成」的说法：用来发现「说了完成、计划却没回标」。
_SYNC_DONE_RE = re.compile(
    r"完成|已通过|已跑|跑通|全绿|已改|已补|已实现|已同步|通过|PASS|done", re.I)
# 同一句里出现这些否定 / 未完成说法，就不算「宣称完成」。
_SYNC_NEG_RE = re.compile(
    r"未完成|没完成|没做完|未做|没做|未跑|没跑|还没|尚未|未通过|待做|待办|待定|待补|待跑|待改|待验证|失败|FAIL",
    re.I)
# 「0 FAIL / 零失败」是成功口径，不能当否定词。
_SYNC_OK_RE = re.compile(r"0\s*FAIL|FAIL\s*0|FAIL\s*数?\s*(?:为)?\s*0|零失败|0\s*(?:个)?失败", re.I)
# 步骤 id（S1 / M1S2 / S10）：前后不能再接字母数字，避免命中变量名。
_STEP_ID_RE = re.compile(r"(?<![A-Za-z0-9])M?\d*S\d+(?![0-9A-Za-z])")


def plan_sync_gaps(plan, text, limit=3):
    """正文说某步已完成 / 已通过，但计划里还没标 done → 返回这些步骤 id。

    现场 s_2809 idx904：正文写「S4 已完成。我更新计划」，计划卡里 S4 仍是
    pending，用户看到的就是「明明做完了却还挂着」。
    """
    steps = [s for s in ((plan or {}).get("steps") or []) if isinstance(s, dict)]
    by = {str(s.get("id") or "").upper(): s for s in steps if s.get("id")}
    if not by or not text:
        return []
    gaps = []
    # 按分句（句末 + 逗号）判，避免同句里别的步骤「已通过」牵连到正在做的步骤
    for sent in re.split(r"[。；;！!？?\n，,]", str(text)):
        sent = _SYNC_OK_RE.sub("", sent)
        if _SYNC_NEG_RE.search(sent) or not _SYNC_DONE_RE.search(sent):
            continue
        for m in _STEP_ID_RE.finditer(sent):
            sid = m.group(0).upper()
            st = by.get(sid)
            if not st or str(st.get("status") or "") == "done":
                continue
            if sid not in gaps:
                gaps.append(sid)
                if len(gaps) >= limit:
                    return gaps
    return gaps


def plan_sync_hint(plan, text):
    """计划回标：正文宣称完成、计划里没标 done 时，先把状态补上再往下做。"""
    gaps = plan_sync_gaps(plan, text)
    if not gaps:
        return ""
    return ("【计划回标】正文里已经说到 %s 完成 / 通过，但计划里还没标 done。"
            "本轮先用 plan_patch 的 update_steps 把这些步骤标 done（挂了验收项的顺便记证据），"
            "再继续后面的步骤；否则计划卡会一直显示它们没做完。"
            % "、".join(gaps))


def plan_pending_line(plan, limit=2, width=30):
    """收尾时用一行点名没做完的步骤：「未完成：xxx；yyy；另有 N 步。」"""
    left = [t for t in _pending_steps(plan) if t]
    if not left:
        return ""
    txt = "未完成：" + "；".join(
        (t if len(t) <= width else t[:width] + "…") for t in left[:limit])
    if len(left) > limit:
        txt += "；另有 %d 步" % (len(left) - limit)
    return txt + "。"


# 「跨轮继续同一个计划」：只有明确的继续 / 进度提问才继承上一版计划；
# 用户开了新任务（再做一个 / 换个需求）就重新规划，避免被旧计划带跑。
_NEW_TASK_RE = re.compile(
    r"再做一个|另做一个|重新做|换个(?:需求|任务|项目)|新任务|从头(?:做|开始)|另一个任务")
_CONT_PREFIX_RE = re.compile(
    r"^\s*(?:继续|接着|下一步|然后|还有|开始吧|做吧|继续吧|go\b|ok\b"
    r"|好的?[，,。!！\s]*$|可以[，,。!！\s]*$|行[，,。!！\s]*$"
    r"|开始[，,。!！\s]*$)", re.I)


def plan_can_inherit(text, cont=False, resume=False, meta=False):
    """这一轮要不要把上一版计划装回 `plan_state`（供 plan_patch 继续改）。"""
    if cont or resume or meta:
        return True
    t = str(text or "").strip()
    if not t or len(t) > 240:
        return False
    if _NEW_TASK_RE.search(t):
        return False
    return bool(_CONT_PREFIX_RE.match(t))


def _tail_asks(text, width=80):
    """结尾是否在征求「要不要我继续」——只认继续类，不认需要用户拍板的决策问句。"""
    t = (text or "").strip()
    return any(k in t[-width:] for k in _CONTINUE_ASK)


def _pending_decision(text, width=160):
    """结尾是否是「等你拍板」的决策问句（A 还是 B / 要不要按 X 做）。

    这类问题出现时不能再自动续跑：模型已经把选择权交回用户，
    系统却催它「自己判断」会把等待变成擅自决定。
    """
    lines = [x.strip() for x in str(text or "").splitlines() if x.strip()]
    if not lines:
        return False
    tail = lines[-1]
    if len(tail) <= width and re.search(r"[?？]\s*$", tail):
        if any(k in tail for k in _DECISION_CONTINUE_HINT):
            return False
        if _DECISION_ASK_RE.search(tail):
            return True
        # 「还是」单独出现太宽（「为什么还是慢？」不是提问），必须同时有成对选择的线索
        if "还是" in tail and re.search(r"选|哪个|用|方案|版本", tail):
            return True
    # 最后一句不是问句、但整段是「需要你决定」的清单：
    # 现场 s_2809 idx758 结尾是「你直接说按你的来，我就开写」，
    # 前面三个待定项都带问号，旧逻辑只盯最后一行，于是漏掉了提问卡。
    block = "\n".join(lines[-10:])
    if len(block) > 1000:
        block = block[-1000:]
    if not (_DECISION_SECTION_RE.search(block) or _HANDOFF_RE.search(block)):
        return False
    has_question = bool(re.search(r"[?？]", block))
    has_choices = bool(_DECISION_CHOICE_RE.search(block))
    return has_question or has_choices


def _claims_complete(text):
    t = (text or "").strip()
    return any(k in t[-160:] for k in _COMPLETE)


def _tail_promises_work(text, width=120):
    """结尾是否在描述「下一步要做什么」。

    只在 agent 场景 + 本轮确实动过手时才据此催，避免把教学讲解
    （「接下来我们看第二块」）当成任务没收尾。
    正文以冒号结尾是最强信号：后面本该跟列表 / 代码 / 工具块。
    """
    t = (text or "").strip()
    if t.endswith(("：", ":")):
        return True
    tail = t[-width:]
    if any(k in tail for k in _PROMISE_WORK):
        return True
    if _ACTION_RE.search(tail) or _FUTURE_ACTION_RE.search(tail) or _BARE_ACTION_RE.search(t):
        return True
    return False


def _claims_blocked(text):
    """结尾是否明确表示「做不下去 / 等用户补材料」。

    这一类不是「任务没做完」，而是「再推也没有工具可调」。
    以前它落进「计划还有未完成步骤」→ 续跑，模型只能反复写叙述，
    三轮后触发空转刹车，用户看到的就是那条「本轮没有结论」。
    """
    t = (text or "").strip()
    if not t:
        return False
    if not _BLOCKED_RE.search(t[-200:]):
        return False
    # 同一段里又说「我自己接着做」→ 还能推进，不按受阻处理
    return not any(k in t[-80:] for k in _SELF_CONTINUE)


def auto_continue_reason(plan, visible, tool_count, wrote, goal, first=False,
                         agent_mode=False, plan_stale=0, summary_stop_chars=180):
    """判断「模型想收尾，但任务其实没做完」，返回 {why, msg} 或 None。

    四类触发：
      1. 结尾在问「要不要 / 需要我」—— 这是等指令，不是完成；
      2. 计划里还有未完成步骤 —— 最可靠，直接推它做下一步（仅 agent_mode）；
      3. 正文承诺了下一步动作、本轮却只做了很少（agent_mode 下才判）—— 「说而不做」；
      4. 没有计划、没动过手、用户又给的是开发型任务 —— 只催一次，让它先建计划（仅 agent_mode）。

    非 Agent 会话（agent_mode=False）没有文件 / 命令能力，也就不存在
    「计划没做完」「该动手没动手」这回事：只保留「征询用户」这类与能力无关的触发。

    放行条件（任一）：
      - 结尾明确表示受阻 / 需要用户补材料（催也做不出来）；
      - 结尾是在等用户拍板的决策问句（交给提问卡，不替用户决定）；
      - 写成篇的结论且结尾没有动作承诺；
      - 明确说了「已完成 / 改完了」；
      - 计划连续 plan_stale 次续跑一个字没变 —— 不再拿「计划未完成」当理由。
    """
    text = visible or ""
    todo = _pending_steps(plan)
    # 受阻优先于一切：这是「推不动」，不是「没做完」。
    # 即使结尾还在问「要不要」，等的也是用户补材料，续跑只会空转。
    if _claims_blocked(text):
        return None
    # 等用户拍板的决策问句：不能再催它「自己判断」，否则会把等待变成擅自决定。
    # 交回给 chat.py 的 ask 提醒，让它改用提问卡把选项摆给用户。
    if _pending_decision(text):
        return None
    # 征询「要不要我继续」：这类直接继续做
    if _tail_asks(text):
        return {
            "why": "结尾在征询用户",
            "msg": ("上一轮的结尾在征询用户，但任务还没结束。"
                    "能自己判断的直接做，不要问「要不要」；只有确实需要用户拍板"
                    "（不可逆操作、涉及偏好）时才用 ask_user。"
                    "如果已经完成，明确说「已完成」并给出结论。"),
        }
    # 阶段总结：成段 + 带进度/结论信号 → 允许先收尾（用户要「先输出一段总结」）。
    # 一句「继续。S1：核对…」不够长 / 没有总结信号，仍然按下面的规则续跑。
    if is_checkpoint_summary(text, summary_stop_chars):
        return None
    # 模型明确宣布完成、且写了成篇的结论 → 放行，即使计划里还有没标 done 的步骤。
    # 计划状态是模型自己维护的，它经常忘记回标；这时再续跑只会换来一段重复总结，
    # 连续两次后反而撞上「空转」刹车（实测：5155 字正文，结尾仍在描述下一步）。
    if _claims_complete(text) and len(text) >= 300 and not _tail_promises_work(text):
        return None
    # 成篇总结 + 结尾没有动作承诺 = 已经给出结论，即使没命中完成词表。
    # 实测一次完整交付（2 万字，结尾是「双击 index.html，Ctrl+F5 强刷」）
    # 因为计划 5 步没回标 done，被连推 3 轮后撞上刹车，正文反而被判成「没有结论」。
    # 计划迟迟不推进时（plan_stale ≥ 2）也不再拿它当理由。
    if (len(text) >= 300 and not _tail_promises_work(text)
            and (not todo or plan_stale >= 2)):
        return None
    if agent_mode and todo and plan_stale < 2:
        lines = "\n".join("- " + t for t in todo[:8])
        ready = plan_ready_hint(plan, 3)
        if ready:
            msg = ("上一轮没有调用工具就结束了，但计划还有 %d 步没完成。\n%s\n\n"
                   "本轮直接按上面顺序做：先做第一条；标了「可并行」的可以同一轮一起发。"
                   "做完立刻用 plan_patch 标 done / blocked，不要只写文字总结。"
                   % (len(todo), ready))
        else:
            msg = ("上一轮没有调用工具就结束了，但计划里还有 %d 步没完成：\n%s\n\n"
                   "直接继续做下一步，不要问「要不要」。某一步确实做不了就把该步标成 blocked "
                   "并说明原因；如果已经全部做完，把计划全部标成 done 再给最终结论。"
                   % (len(todo), lines))
        return {"why": "计划还有 %d 步未完成" % len(todo), "msg": msg}
    if _claims_complete(text):
        return None
    # 「说而不做」：这一轮动过手（tool_count≥1），却没有对应的工具调用就收尾了。
    # 只认明确承诺（词表 / 句首引导词 / 将来时动作）：以前「动过手 + 没命中完成词」
    # 是无条件续跑，长任务尾部会一路催到空转刹车。
    if agent_mode and tool_count >= 1 and _tail_promises_work(text):
        return {
            "why": "结尾说了要做但没调用工具",
            "msg": ("你上一轮动过手，但结尾既没有宣布完成，也没有再调用工具，任务就停在那里了。"
                    "现在直接做：读完就改、改完就验证，不要在正文里描述下一步却不执行。"
                    "确实做不了（缺参数、需要用户决定）就明确说卡在哪里，或用 ask_user 问清。"),
        }
    if (agent_mode and first and not tool_count and not wrote
            and any(k in (goal or "") for k in _DEV_GOAL)):
        return {
            "why": "多步任务还没有执行计划",
            "msg": ("这次是多步任务，但还没有执行计划。"
                    "先用 update_plan 列出 3–8 步（每步写清改哪个文件 / 验证什么），再按计划执行；"
                    "如果确实只需要一步，就直接做完并说明，不要停在询问上。"),
        }
    return None

