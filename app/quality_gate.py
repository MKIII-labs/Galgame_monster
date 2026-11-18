# -*- coding: utf-8 -*-
"""验收子代理（可信保真闸门）。

主模型说「做完了」不等于真的做完了。这里另起一个**独立上下文的 AI**：
- 与主模型拥有相同的工具权限（读 / 写 / 跑命令，写与命令仍走主循环审批）；
- 它自己去看产物、读文件、跑命令，不看主模型的自评分；
- 按可核验的标准逐条判定，输出结构化 JSON；
- 客观硬伤（工具失败未修复 / 文件消失 / 计划未完成 / 验收项未通过 / 漏核对计划条件）
  直接判不通过 —— 模型再怎么说好也过不去；
- 不打分：判定只看验收项是否通过且带证据。

和旧的「像素闸门」的区别：不依赖 image_compare，也不靠主模型自报；覆盖所有任务。
"""
import json
import re
import time

from app.context import TOOL_MSG_PREFIX

MAX_ROUNDS = 12              # 验收代理自己的工具轮次上限（0 = 不限）
RUN_TIMEOUT = 180.0          # 单次验收最长多久
MAX_BRIEF_TOOLS = 40         # 工具流水最多列多少条
MAX_CRITERIA = 12            # 验收项最多几条（计划条件最多 8 条，还要留出自行补充的）
MAX_CHECKS = 8               # 计划里自带的验收条件最多核对几条

# 验收项来源 / 强度：
#   user     = 用户任务里明确要求的结果（锁定：不可删、不可降级、不可申诉）
#   plan     = 计划里写明的验收条件（blocking，但主模型可以申诉）
#   reviewer = 验收代理自行补充的检查（默认 advisory，只进建议，不否决交付）
SOURCES = ("user", "plan", "reviewer")
SEVERITIES = ("blocking", "advisory")

# 申诉类型：主模型必须说明「为什么这一项在实际环境下不该判不通过」，不接受空口申诉。
CHALLENGE_KINDS = ("env_impossible", "not_in_user_scope", "plan_drift", "already_covered")
MAX_CHALLENGES = 2           # 每回合最多申诉几条（同一项每回合只允许一次）

# 验收文本归一化：只去掉空白与标点，保留中英文与数字，用于「计划条件是否被核对」的匹配
_TEXT_NOISE = re.compile(r"[\s，。；：、,.;:!?！？（）()\[\]【】\"'“”‘’/\\|]+")

# 验收代理不拿这些工具：它们要么会改写任务状态，要么会把主对话挂起，
# 要么会再派生一层子代理（判定的成本会失控）。
# 元工具重构后 schema 下发的是 plan / agent / workflow 这类分组名，旧名保留兼容。
DENY_TOOLS = frozenset((
    "finish", "ask_user", "update_plan", "plan_patch", "acceptance",
    "explore", "explore_followup", "explore_status", "explore_result",
    "explore_cancel", "delegate", "shell", "sleep", "set_reminder",
    "todo_write", "read_tool_result",
    # 元工具名：status 由子命令决定，整组不给验收代理
    "plan", "agent", "workflow", "utility",
))

REVIEW_SYSTEM = "\n".join([
    "你是独立的验收代理。你不是主代理：不要接着做任务，只负责判定交付是否真的达标。",
    "你有和主代理相同的工具权限（读文件 / 写文件 / 跑命令），但原则上**不要改文件**；"
    "只有在验证必须时（例如跑测试、生成对比图）才执行命令，每次都会经过用户审批。",
    "",
    "判定要求：",
    "1. 自己核对：把任务要求与磁盘上的真实产物对比。主代理的自述只能当线索，不能当证据。",
    "2. 验收项只来自三处，并给每条标 source / severity：",
    "   - source=user：用户任务里明确要求的结果。severity 必须是 blocking；"
    "用户要求不可删、不可降级、不可申诉。",
    "   - source=plan：任务书「计划里写明的验收条件」，逐字照抄。severity 必须是 blocking，"
    "但主模型可以申诉。",
    "   - source=reviewer：你自己补充的检查（1–4 条）。severity 默认 advisory —— "
    "它只作为改进建议，**不会**否决交付；只有确实属于交付缺陷时才标 blocking。",
    "   计划步骤里的 verify 只是核对方式参考，不要把它逐条变成独立验收项。",
    "3. 逐条判 passed；**每条 passed 必须带证据**"
    "（文件:行号，或命令 + 关键输出片段），没有证据的一律按不通过计。",
    "4. 不打分：判定只取决于 blocking 项是否全部 passed 且带证据；"
    "advisory 项未通过只写进 gaps 当建议，不能作为不通过的理由。",
    "5. 环境本身不具备的条件（例如沙箱不是 git 仓库、本地文件不支持 http 打开、"
    "缺少用户才能提供的参考物）不是交付缺陷：按等价方式核对，或整体判 blocked；"
    "**不要要求主代理反复重试注定失败的命令**。",
    "6. 判 pass 时 gaps 必须是空数组：通过之后不再给「建议补做」的事项，"
    "否则主代理会为不存在的差距反复返工。",
    "7. 无法验证时（缺参考物 / 需要人工审美 / 环境不可用）用 verdict=blocked，并写清原因。",
    "8. 只输出一个 JSON 对象，前后不要写任何解释文字：",
    '{"verdict":"pass|fail|blocked",'
    '"criteria":[{"criterion":"验收项","passed":true,"evidence":"文件:行号 或 命令输出",'
    '"source":"user|plan|reviewer","severity":"blocking|advisory"}],'
    '"gaps":["还差什么（可执行的一句话）"],"next_action":"给主代理的一条下一步指令",'
    '"challenge_results":[{"criterion":"被申诉的验收项","accepted":false,'
    '"reason":"为什么接受 / 不接受"}]}',
    "",
    "如果任务书给出了「计划里写明的验收条件」，必须为每一条单独列一个 criterion，"
    "文本逐字照抄，source=plan，并独立核对；漏掉任何一条（已通过申诉降级的除外）"
    "都会按未通过计。",
    "如果任务书给出了「主模型的申诉」，必须逐条裁决并写进 challenge_results："
    "接受 → 把对应 criterion 改成 source=reviewer / severity=advisory（或从清单删掉），"
    "并写清理由；不接受 → 保留 blocking，并写清还缺什么证据。"
    "source=user 的项一律不接受申诉。",
    "工具名必须和任务书【可用工具】完全一致；不要凭记忆写旧名，"
    "不确定就先看列表再调用。",
    "禁止：不要说「看起来 / 应该 / 大概」；没查就写「未核实」。"
    "gaps 最多 5 条，按影响从大到小排。",
])

FACTS_ONLY_NOTE = (
    "本轮你没有可用的工具，只能依据下面列出的客观事实判定；"
    "事实里没有覆盖到的验收项一律写 passed=false（evidence 写「未核实」）。"
)


def _clip(text, n):
    t = str(text or "").strip()
    if len(t) <= n:
        return t
    return t[:max(0, n - 1)].rstrip() + "…"


def _source(value, default="plan"):
    s = str(value or "").strip().lower()
    return s if s in SOURCES else default


def _severity(value, source):
    """用户要求必须是 blocking；其它项按模型给的值，缺省按来源推断。"""
    if source == "user":
        return "blocking"
    s = str(value or "").strip().lower()
    if s in SEVERITIES:
        return s
    return "blocking" if source == "plan" else "advisory"


def _criterion(item, passed, evidence="", source="plan", severity="", note=""):
    src = _source(source)
    return {"criterion": _clip(item, 160), "passed": bool(passed),
            "evidence": _clip(evidence, 300), "note": note,
            "source": src, "severity": _severity(severity, src),
            "locked": src == "user"}


def _norm(text):
    return _TEXT_NOISE.sub("", str(text or "")).lower()


def _match_criterion(text, criteria):
    """按归一化文本找验收项（允许轻微改写）；找不到返回 None。"""
    n = _norm(text)
    if not n:
        return None
    loose = None
    for c in criteria or []:
        if not isinstance(c, dict):
            continue
        cn = _norm(c.get("criterion"))
        if not cn:
            continue
        if cn == n:
            return c
        if n in cn or (len(cn) >= 4 and cn in n):
            loose = loose or c
    return loose


def _matches_check(text, checks):
    n = _norm(text)
    if not n:
        return False
    for chk in checks or []:
        cn = _norm((chk or {}).get("criterion") if isinstance(chk, dict) else chk)
        if cn and (n == cn or n in cn or (len(cn) >= 4 and cn in n)):
            return True
    return False


def _sig(entry):
    try:
        return json.dumps(entry.get("args") or {}, sort_keys=True,
                          ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(entry.get("args"))


# 环境 / 前提不成立导致的失败：原样重试也不会成功，不能算「未修复的失败」。
# 现场：沙箱不是 git 仓库时 `git status` 永远失败，旧逻辑却要求「同名同参重试成功」
# 才算解决，主代理只能一轮轮重跑注定失败的命令。
_NON_RETRYABLE = re.compile(
    r"(not a git repository|只支持 http|url 只接受 http|路径越出沙箱|"
    r"不存在|not found|no such file|command not found|不是可预览|"
    r"禁止访问|密钥 / 凭据|需要管理员|没有权限|拒绝 1024)",
    re.I)


def _non_retryable(result):
    return bool(_NON_RETRYABLE.search(str(result or "")))


def unresolved_failures(tool_log):
    """失败的调用之后没有「同名同参且成功」的重试 → 视为未解决的失败。

    前提不成立的失败（不是 git 仓库 / 本地文件不支持 http 等）直接跳过：
    它们的「修复方式」不是重试，而是换一种验证途径。
    """
    entries = [e for e in (tool_log or []) if isinstance(e, dict)]
    out = []
    for i, e in enumerate(entries):
        if e.get("ok", True) or _non_retryable(e.get("result")):
            continue
        name, sig = str(e.get("tool") or ""), _sig(e)
        fixed = any((x.get("ok", True) and str(x.get("tool") or "") == name
                     and _sig(x) == sig) for x in entries[i + 1:])
        if not fixed:
            out.append({"tool": name,
                        "result": _clip(e.get("result") or "", 200)})
    return out


def plan_checks(plan):
    """计划里写明的验收条件：整体 done_when + 里程碑 done_when。

    只收「目标级」条件。步骤里的 verify 是核对方式，不是验收项 —— 以前把它也
    塞进来，结果 `git status`（沙箱不是仓库）、`open_preview 不报错`（本地文件
    不支持 http）这类前提不成立的命令被当成硬性验收项，主代理只能反复重试。
    每条都标 source=plan / severity=blocking：必须核对，但主模型可以申诉。
    """
    out, seen = [], set()

    def _add(text):
        t = _clip(text, 160)
        if t and t not in seen:
            seen.add(t)
            out.append({"criterion": t, "source": "plan", "severity": "blocking"})

    plan = plan if isinstance(plan, dict) else {}
    for t in plan.get("done_when") or []:
        _add(t)
    for m in plan.get("milestones") or []:
        if isinstance(m, dict):
            _add(m.get("done_when"))
    return out[:MAX_CHECKS]


def plan_hints(plan):
    """步骤里的 verify：给验收代理参考「怎么查」，不作为独立验收项。"""
    out, seen = [], set()
    for s in (plan or {}).get("steps") or []:
        if not isinstance(s, dict):
            continue
        t = _clip(s.get("verify"), 120)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:MAX_CHECKS]


def uncovered_checks(checks, criteria):
    """计划条件里没有被报告「逐条提到」的项（报告需照抄文本，允许轻微改写）。

    返回计划条件原样的 dict 列表（含 source / severity），由调用方区分
    blocking 与 advisory：blocking 漏核对才算硬伤，advisory 漏核对只是建议。
    """
    got = [_norm(c.get("criterion") or "") for c in (criteria or [])
           if isinstance(c, dict)]
    out = []
    for chk in (checks or []):
        item = (dict(chk) if isinstance(chk, dict)
                else {"criterion": str(chk or ""), "source": "plan",
                      "severity": "blocking"})
        n = _norm(item.get("criterion"))
        if not n:
            continue
        if not any(n in g or (len(g) >= 4 and g in n) for g in got):
            out.append(item)
    return out


def build_facts(tool_log=None, plan=None, criteria=None, pending_accept=None,
                missing_files=None, written=0, changed_files=None):
    """把客观事实收敛成一份 dict（只陈述事实，不做评价）。"""
    steps = (plan or {}).get("steps") or []
    plan_pending = [str(s.get("id") or "") for s in steps
                    if isinstance(s, dict) and str(s.get("status") or "") != "done"]
    return {
        "unresolved": unresolved_failures(tool_log),
        "missing_files": [str(x) for x in (missing_files or []) if str(x).strip()][:20],
        "plan_pending": plan_pending[:12],
        "accept_pending": [str((c or {}).get("id") or "") for c in (pending_accept or [])
                           if str((c or {}).get("id") or "")][:12],
        "written": int(written or 0),
        "changed_files": [str(x) for x in (changed_files or []) if str(x).strip()][:20],
        "criteria": [c for c in (criteria or []) if isinstance(c, dict)][:MAX_CRITERIA],
        "plan_checks": plan_checks(plan),
        "plan_hints": plan_hints(plan),
        # 完成判据的修订记录：验收代理据此知道「标准中途变过」，不用旧标准卡人
        "done_when_rev": [dict(x) for x in ((plan or {}).get("done_when_rev") or [])
                          if isinstance(x, dict)][-5:],
    }


def parse_report(text):
    """从模型输出里取结构化报告；格式不合法返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    blob = m.group(1) if m else None
    if blob is None:
        i, j = raw.find("{"), raw.rfind("}")
        blob = raw[i:j + 1] if 0 <= i < j else None
    if not blob:
        return None
    try:
        obj = json.loads(blob)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "fail", "blocked"):
        verdict = "fail"
    criteria = []
    for c in (obj.get("criteria") or [])[:MAX_CRITERIA]:
        if isinstance(c, str):
            c = {"criterion": c}
        if not isinstance(c, dict):
            continue
        name = str(c.get("criterion") or c.get("item") or c.get("text") or "").strip()
        if not name:
            continue
        passed = bool(c.get("passed"))
        evidence = str(c.get("evidence") or "").strip()
        note = ""
        if passed and not evidence:
            # 证据强制：没有证据的「通过」不算通过
            passed, note = False, "通过但未提供证据"
        criteria.append(_criterion(
            name, passed, evidence,
            source=c.get("source") or "plan",      # 漏标按 plan：仍会卡，但可以申诉
            severity=c.get("severity"), note=note))
    gaps = [_clip(g, 200) for g in (obj.get("gaps") or []) if str(g or "").strip()][:5]
    challenges = []
    for r in (obj.get("challenge_results") or [])[:MAX_CHALLENGES]:
        if not isinstance(r, dict):
            continue
        name = str(r.get("criterion") or "").strip()
        if not name:
            continue
        challenges.append({"criterion": _clip(name, 160),
                           "accepted": bool(r.get("accepted")),
                           "reason": _clip(r.get("reason"), 200)})
    return {
        "verdict": verdict,
        "criteria": criteria,
        "gaps": gaps,
        "challenge_results": challenges,
        "next_action": _clip(obj.get("next_action") or "", 300),
        "valid": bool(criteria) or verdict == "blocked",
    }


def normalize_challenges(raw, criteria, used=None):
    """校验主模型提交的申诉；返回 (可受理的申诉, 拒绝原因列表)。

    `used` 是「本回合每条验收项已申诉次数」：同一项每回合只允许一次。
    用户明确要求的项（source=user）不允许申诉；只能对准最近一次未通过的项。
    """
    used = used if isinstance(used, dict) else {}
    crits = [c for c in (criteria or []) if isinstance(c, dict)]
    out, errs = [], []
    total = sum(int(v or 0) for v in used.values())
    seen = set()
    for i, c in enumerate(raw or [], 1):
        if not isinstance(c, dict):
            errs.append("第 %d 条申诉不是对象。" % i)
            continue
        text = str(c.get("criterion") or c.get("item") or "").strip()
        kind = str(c.get("type") or c.get("kind") or "").strip().lower()
        ev = str(c.get("evidence") or "").strip()
        if not text:
            errs.append("第 %d 条申诉缺少 criterion。" % i)
            continue
        if kind not in CHALLENGE_KINDS:
            errs.append("第 %d 条申诉的 type 不支持：%s（可用：%s）"
                        % (i, kind or "（空）", "、".join(CHALLENGE_KINDS)))
            continue
        if len(ev) < 4:
            errs.append("第 %d 条申诉需要 evidence：写清为什么这条验收项在实际环境下"
                        "不该判不通过。" % i)
            continue
        target = _match_criterion(text, crits)
        if not target:
            errs.append("第 %d 条申诉对应的验收项不在最近一次未通过清单里：%s"
                        % (i, _clip(text, 60)))
            continue
        if target.get("source") == "user" or target.get("locked"):
            errs.append("第 %d 条是用户明确要求的验收项，不能申诉或降级：%s"
                        % (i, _clip(target.get("criterion"), 60)))
            continue
        key = _norm(target.get("criterion"))
        if used.get(key) or key in seen:
            errs.append("第 %d 条本回合已经申诉过，不能重复：%s"
                        % (i, _clip(target.get("criterion"), 60)))
            continue
        if total + len(out) >= MAX_CHALLENGES:
            errs.append("本回合最多申诉 %d 条，已到上限。" % MAX_CHALLENGES)
            break
        seen.add(key)
        out.append({"criterion": str(target.get("criterion") or text),
                    "type": kind, "evidence": _clip(ev, 300),
                    "key": key})
    return out, errs


def merge_report(report, facts):
    """逐项证据 + 客观硬伤 → 最终判定（不再有分数与及格线）。

    通过条件：blocking 验收项全部 passed、无客观硬伤；advisory 未过只进建议。
    """
    rep = dict(report or {})
    facts = facts or {}
    # 归一化来源 / 强度：计划条件对应的项强制回 blocking —— 验收代理不能靠改
    # source 把计划要求降级；用户明确要求的项（source=user）一律锁定。
    criteria = []
    for c in rep.get("criteria") or []:
        if not isinstance(c, dict):
            continue
        src = _source(c.get("source"))
        item = dict(c)
        item.update({"source": src,
                     "severity": _severity(c.get("severity"), src),
                     "locked": src == "user"})
        criteria.append(item)
    for c in criteria:
        if _matches_check(c.get("criterion"), facts.get("plan_checks") or []):
            c.update({"source": "plan", "severity": "blocking", "locked": False})
    rep["criteria"] = criteria

    # 申诉裁决：接受 → 降级为 advisory（不再否决）；拒绝 → 记录「不许再返工」。
    ch_res = [r for r in (rep.get("challenge_results") or []) if isinstance(r, dict)]
    for r in ch_res:
        target = _match_criterion(r.get("criterion"), criteria)
        if not target:
            continue
        if target.get("locked"):
            r["accepted"] = False
            r["reason"] = (str(r.get("reason") or "")
                           or "用户明确要求的验收项不能通过申诉降级。")
            continue
        if r.get("accepted"):
            target["severity"] = "advisory"
            target["note"] = "申诉通过：" + str(r.get("reason") or "")
        else:
            target["appeal_rejected"] = True
    # 主模型提了申诉、验收代理却没写 challenge_results：按「未接受」处理。
    submitted = [str(x) for x in (facts.get("challenges") or []) if str(x).strip()]
    answered = {_norm(r.get("criterion")) for r in ch_res}
    unanswered = [x for x in submitted if _norm(x) not in answered]
    if unanswered:
        for c in criteria:
            cn = _norm(c.get("criterion"))
            if any(cn and (cn == _norm(x) or _norm(x) in cn or cn in _norm(x))
                   for x in unanswered):
                c["appeal_rejected"] = True

    hard = []
    if facts.get("missing_files"):
        hard.append("写过的文件在磁盘上找不到：%s"
                    % "、".join(facts["missing_files"][:4]))
    if facts.get("unresolved"):
        hard.append("%d 个工具调用失败后没有修复" % len(facts["unresolved"]))
    if facts.get("plan_pending"):
        hard.append("计划还有 %d 步没完成" % len(facts["plan_pending"]))
    if facts.get("accept_pending"):
        hard.append("验收项未通过：%s" % "、".join(facts["accept_pending"][:4]))
    # 计划里写明的 blocking 条件必须逐条被核对；漏项等于没验。
    # 已通过申诉的项不算漏核对（验收代理可能直接把它从清单里删掉了）。
    missing = uncovered_checks(facts.get("plan_checks") or [], criteria)
    accepted = [r for r in ch_res if r.get("accepted")]
    missing_block = [x for x in missing if x.get("severity") == "blocking"
                     and not _matches_check(x.get("criterion"), accepted)]
    missing_adv = [x for x in missing if x.get("severity") != "blocking"
                   and not _matches_check(x.get("criterion"), accepted)]
    if missing_block:
        hard.append("没有核对计划里的验收条件：%s"
                    % "、".join(str(x.get("criterion")) for x in missing_block[:3]))

    verdict = str(rep.get("verdict") or "fail")
    blocking_failed = [c for c in criteria
                       if not c.get("passed") and c.get("severity") == "blocking"]
    advisory_failed = [c for c in criteria
                       if not c.get("passed") and c.get("severity") != "blocking"]
    # 空报告（没有 criteria）不能因为「没有 blocking 失败项」反而判过
    valid = bool(rep.get("valid", True)) and (bool(criteria) or verdict == "blocked")
    passed = (valid and verdict in ("pass", "fail")
              and not blocking_failed and not hard)
    if passed:
        verdict = "pass"
    elif verdict == "pass":
        verdict = "fail"
    # 申诉被驳回后不再对同一项返工：剩下的失败项全是「申诉未采纳」→ 交用户裁决
    arbitrate = bool(blocking_failed) and all(
        c.get("appeal_rejected") for c in blocking_failed)
    gaps = []
    if not passed:
        gaps = list(rep.get("gaps") or [])
        for h in hard:
            if h not in gaps:
                gaps.append(h)
        for c in blocking_failed:
            t = "未通过：" + str(c.get("criterion") or "")
            if t not in gaps:
                gaps.append(t)
    suggestions = []
    for c in advisory_failed:
        t = str(c.get("criterion") or "")
        if c.get("note"):
            t += "（" + str(c["note"]) + "）"
        suggestions.append(t)
    for x in missing_adv:
        suggestions.append(str(x.get("criterion")))
    # 通过之后不再输出「建议补做」：模型会把它当成未完成的差距反复返工
    if passed:
        suggestions = list(rep.get("gaps") or []) + suggestions
    rep.update({"verdict": verdict, "gaps": gaps[:6],
                "suggestions": suggestions[:5],
                # blocked = 环境无法验证；arbitrate = 申诉僵持，重试都不会变好
                "terminal": verdict == "blocked" or arbitrate,
                "arbitrate": arbitrate,
                "next_action": "" if passed else rep.get("next_action"),
                "facts": facts, "passed": passed})
    return rep


def _log_brief(tool_log):
    lines = []
    for e in (tool_log or [])[-MAX_BRIEF_TOOLS:]:
        if not isinstance(e, dict):
            continue
        state = "ok" if e.get("ok", True) else "FAIL"
        lines.append("- %s [%s] %s" % (e.get("tool") or "?", state,
                                       _clip(e.get("result") or "", 160)))
    return "\n".join(lines) or "（本轮没有工具调用）"


def tool_lines(schemas):
    """schema → 「名字 + action」清单，写进任务书让验收代理照着调用。"""
    rows = []
    for sc in schemas or []:
        fn = sc.get("function") if isinstance(sc, dict) else None
        name = str((fn or {}).get("name") or "").strip()
        if not name:
            continue
        props = ((fn.get("parameters") or {}).get("properties") or {})
        acts = (props.get("action") or {}).get("enum") or []
        suffix = ("（action: " + " / ".join(str(a) for a in acts) + "）") if acts else ""
        rows.append("- " + name + suffix)
    return rows


def allowed_names(schemas):
    """可用工具名 + 能映射到它们的旧名。

    元工具重构后 schema 是 read / edit / run…，模型常沿用 read_file / run_command。
    执行端 `_wf_exec_node` 两种名字都能跑；这里只把白名单放宽，避免把能执行的旧名
    误判成「不在可用范围内」（现场：验收代理连续 5 次工具调用全被拒）。
    """
    names = {str((s.get("function") or {}).get("name") or "") for s in schemas or []}
    names.discard("")
    try:
        from app import tool_meta
        for legacy, (meta, _action) in tool_meta.legacy_map().items():
            if meta in names:
                names.add(legacy)
    except Exception:                     # noqa: BLE001  元层不可用时只认原名
        pass
    return names


def build_brief(task, plan_text="", tool_log=None, facts=None, summary="",
                answer="", sandbox="", has_tools=True, tools=None, challenges=None):
    """给验收代理的任务书。**不传主模型的自我评价**，只传事实与待核对的声明。"""
    facts = facts or {}
    parts = [
        "【用户的任务】\n" + _clip(task or "", 1200),
        "【沙箱根目录】\n" + (sandbox or "（没有沙箱）"),
        "【主代理的计划】\n" + (_clip(plan_text, 2000) or "（本轮没有计划）"),
        "【本轮工具流水（按时间顺序，FAIL = 调用失败）】\n" + _log_brief(tool_log),
    ]
    _rows = tool_lines(tools)
    if _rows:
        parts.append("【可用工具（只能调用这些名字）】\n" + "\n".join(_rows))
    if facts.get("plan_checks"):
        parts.append("【计划里写明的验收条件（必须逐条照抄成 criterion，source=plan）】\n"
                     + "\n".join("- " + str(x.get("criterion"))
                                 for x in facts["plan_checks"]))
    if facts.get("plan_hints"):
        parts.append("【核对方式参考（计划步骤的 verify，不是独立验收项）】\n"
                     + "\n".join("- " + x for x in facts["plan_hints"]))
    if facts.get("done_when_rev"):
        rows = []
        for rev in facts["done_when_rev"]:
            old = "；".join(str(x) for x in (rev.get("old") or [])) or "（空）"
            new = "；".join(str(x) for x in (rev.get("new") or [])) or "（空）"
            rows.append("- 原：%s → 新：%s（原因：%s）"
                        % (old, new, rev.get("reason") or "未说明"))
        parts.append("【完成判据修订记录（以最新值为准，不要用旧标准卡人）】\n"
                     + "\n".join(rows))
    if facts.get("changed_files"):
        parts.append("【本轮写过的文件】\n" + "、".join(facts["changed_files"]))
    obj = []
    if facts.get("unresolved"):
        obj.append("未修复的失败：" + "；".join(
            "%s（%s）" % (x.get("tool"), x.get("result")) for x in facts["unresolved"][:3]))
    if facts.get("missing_files"):
        obj.append("写过后在磁盘上找不到：" + "、".join(facts["missing_files"][:6]))
    if facts.get("plan_pending"):
        obj.append("计划未完成步骤：" + "、".join(facts["plan_pending"]))
    if facts.get("accept_pending"):
        obj.append("未通过的验收项：" + "、".join(facts["accept_pending"]))
    parts.append("【系统核对的客观事实】\n" + ("\n".join(obj) or "（没有发现硬伤）"))
    if challenges:
        rows = []
        for c in challenges:
            rows.append("- 验收项：%s\n  类型：%s\n  证据：%s"
                        % (c.get("criterion"), c.get("type"), c.get("evidence")))
        parts.append("【主模型的申诉（必须逐条裁决并写进 challenge_results）】\n"
                     + "\n".join(rows))
    if summary:
        parts.append("【主代理的完成声明（仅供参考，不能当证据）】\n" + _clip(summary, 800))
    if answer:
        parts.append("【主代理给用户的回答（待核对）】\n" + _clip(answer, 2000))
    if not has_tools:
        parts.append("【限制】" + FACTS_ONLY_NOTE)
    parts.append("现在开始独立验收。只输出 JSON。")
    return "\n\n".join(parts)


def _default_chat(ctx, messages, schemas, stop_event):
    from app import explore
    return explore._default_chat(ctx, messages, schemas, stop_event)


def run(brief, ctx):
    """跑一次验收。ctx: chat_fn / schemas / tool_fn / emit / stop_event / model。

    返回 {ok, report, raw, error, rounds, tokens, ms, tools}。
    """
    ctx = dict(ctx or {})
    emit = ctx.get("emit") or (lambda o: None)
    stop = ctx.get("stop_event")
    chat_fn = ctx.get("chat_fn") or _default_chat
    schemas = list(ctx.get("schemas") or [])
    tool_fn = ctx.get("tool_fn")
    names = allowed_names(schemas)
    messages = [{"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": brief}]
    try:
        max_rounds = int(ctx.get("max_rounds") or 0)
    except (TypeError, ValueError):
        max_rounds = MAX_ROUNDS
    max_rounds = max_rounds if max_rounds > 0 else 10 ** 9
    try:
        timeout = float(ctx.get("timeout") or RUN_TIMEOUT)
    except (TypeError, ValueError):
        timeout = RUN_TIMEOUT
    t0, rounds, tokens, used, text, err = time.monotonic(), 0, 0, [], "", ""

    def _think():
        if stop is not None and stop.is_set():
            return "用户已停止本轮"
        if time.monotonic() - t0 > timeout:
            return "验收超时"
        return ""

    try:
        while rounds < max_rounds:
            why = _think()
            if why:
                err = why
                break
            rr = chat_fn(ctx, messages, schemas, stop)
            rounds += 1
            try:
                tokens += int((rr.get("usage") or {}).get("total_tokens") or 0)
            except (TypeError, ValueError):
                pass
            calls = rr.get("tool_calls") or []
            if calls:
                if rr.get("visible"):
                    messages.append({"role": "assistant", "content": rr["visible"]})
                for call in calls:
                    name = str((call or {}).get("tool") or "").strip()
                    args = (call or {}).get("args") or {}
                    if not name:
                        continue
                    if name not in names and names:
                        messages.append({"role": "user", "content":
                                         TOOL_MSG_PREFIX + "\n【%s】不在你的可用范围内。" % name})
                        continue
                    emit({"type": "review_tool", "name": name})
                    try:
                        ok, out = tool_fn(name, args) if callable(tool_fn) else (
                            False, "验收代理没有工具执行器，只能依据任务书里的事实判定。")
                    except Exception as e:         # noqa: BLE001
                        ok, out = False, "工具执行失败：%s" % e
                    used.append(name)
                    messages.append({"role": "user", "content":
                                     TOOL_MSG_PREFIX + "\n【%s】%s\n%s"
                                     % (name, "" if ok else "❌ ", _clip(out, 4000))})
                continue
            text = str(rr.get("visible") or "").strip()
            break
    except Exception as e:                         # noqa: BLE001
        err = "验收代理失败：%s" % e

    report = parse_report(text)
    # 格式 / 证据不合格：给它一次改正机会
    if text and report is None and not err and _think() == "" and rounds < max_rounds:
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content":
                         "上面的输出不是合法 JSON。只重发一个 JSON 对象，不要解释。"})
        try:
            rr = chat_fn(ctx, messages, schemas, stop)
            rounds += 1
            try:
                tokens += int((rr.get("usage") or {}).get("total_tokens") or 0)
            except (TypeError, ValueError):
                pass
            text = str(rr.get("visible") or "").strip()
            report = parse_report(text)
        except Exception as e:                     # noqa: BLE001
            err = err or ("验收代理失败：%s" % e)

    ms = int((time.monotonic() - t0) * 1000)
    ok = bool(report and report.get("valid")) and not err
    return {"ok": ok, "report": report, "raw": text, "error": err,
            "rounds": rounds, "tokens": tokens, "ms": ms, "tools": used}


def review_text(report, max_gaps=4):
    """把最终判定压成给主模型 / 用户看的一段话。"""
    rep = report or {}
    crit = [c for c in (rep.get("criteria") or []) if isinstance(c, dict)]
    failed = [c for c in crit if not c.get("passed")
              and c.get("severity") == "blocking"]
    adv = [c for c in crit if not c.get("passed")
           and c.get("severity") != "blocking"]
    head = "【验收子代理】%s · %d/%d 项通过" % (
        {"pass": "通过", "blocked": "无法验证"}.get(rep.get("verdict"), "未通过"),
        len(crit) - len([c for c in crit if not c.get("passed")]), len(crit))
    # 通过就只报结论：再附「差距 / 下一步」会让主代理为不存在的差距继续返工
    if rep.get("passed"):
        return head
    lines = [head]
    for c in failed[:4]:
        ev = ("  证据：" + c.get("evidence")) if c.get("evidence") else ""
        lines.append("  ✗ " + str(c.get("criterion") or "") + ev)
    if adv:
        lines.append("建议（不阻塞）：" + "；".join(
            str(c.get("criterion") or "") for c in adv[:3]))
    gaps = rep.get("gaps") or []
    if gaps:
        lines.append("差距：" + "；".join(str(g) for g in gaps[:max_gaps]))
    if rep.get("next_action"):
        lines.append("下一步：" + str(rep["next_action"]))
    if rep.get("arbitrate"):
        lines.append("申诉未被采纳：已停止对同一项返工，请你在验收卡上裁决。")
    return "\n".join(lines)
