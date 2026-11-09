# -*- coding: utf-8 -*-
"""验收子代理（可信保真闸门）。

主模型说「做完了」不等于真的做完了。这里另起一个**独立上下文的 AI**：
- 与主模型拥有相同的工具权限（读 / 写 / 跑命令，写与命令仍走主循环审批）；
- 它自己去看产物、读文件、跑命令，不看主模型的自评分；
- 按可核验的标准逐条判定，输出结构化 JSON；
- 客观事实（工具失败未修复 / 文件消失 / 计划未完成 / 验收项未通过 / 像素分不达标）
  会给分数封顶 —— 模型再怎么说好也过不去。

和旧的「像素闸门」的区别：不依赖 image_compare，也不靠主模型自报；覆盖所有任务。
"""
import json
import re
import time

from app.context import TOOL_MSG_PREFIX

MIN_SCORE = 70               # 默认及格线（配置 agent_acceptance_min）
MAX_ROUNDS = 12              # 验收代理自己的工具轮次上限（0 = 不限）
RUN_TIMEOUT = 180.0          # 单次验收最长多久
MAX_BRIEF_TOOLS = 40         # 工具流水最多列多少条
MAX_CRITERIA = 12            # 验收项最多几条（计划条件最多 8 条，还要留出自行补充的）
MAX_CHECKS = 8               # 计划里自带的验收条件最多核对几条

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
    "2. 先列验收项：任务书给出的计划条件逐条照抄，再补 1–4 条自己的检查（总共不超过 12 条）；"
    "逐条判 passed；**每条 passed 必须带证据**"
    "（文件:行号，或命令 + 关键输出片段），没有证据的一律按不通过计。",
    "3. 打分 0–100（及格线见下方），分数要和验收项一致：有未通过项就不能给高分。",
    "4. 无法验证时（缺参考物 / 需要人工审美 / 环境不可用）用 verdict=blocked，并写清原因。",
    "5. 只输出一个 JSON 对象，前后不要写任何解释文字：",
    '{"verdict":"pass|fail|blocked","score":0,'
    '"criteria":[{"criterion":"验收项","passed":true,"evidence":"文件:行号 或 命令输出"}],'
    '"gaps":["还差什么（可执行的一句话）"],"next_action":"给主代理的一条下一步指令"}',
    "",
    "如果任务书给出了「计划里写明的验收条件」，必须为每一条单独列一个 criterion，"
    "文本逐字照抄，并独立核对；漏掉任何一条都会按未通过计。",
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


def _sig(entry):
    try:
        return json.dumps(entry.get("args") or {}, sort_keys=True,
                          ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(entry.get("args"))


def unresolved_failures(tool_log):
    """失败的调用之后没有「同名同参且成功」的重试 → 视为未解决的失败。"""
    entries = [e for e in (tool_log or []) if isinstance(e, dict)]
    out = []
    for i, e in enumerate(entries):
        if e.get("ok", True):
            continue
        name, sig = str(e.get("tool") or ""), _sig(e)
        fixed = any((x.get("ok", True) and str(x.get("tool") or "") == name
                     and _sig(x) == sig) for x in entries[i + 1:])
        if not fixed:
            out.append({"tool": name,
                        "result": _clip(e.get("result") or "", 200)})
    return out


def plan_checks(plan):
    """计划里写明的验收条件：整体 done_when + 里程碑 done_when + 每步 verify。

    这些是「任务自带的验收合同」，收尾复核必须逐条核对；但它们允许在执行中
    通过 plan_patch 更新 —— 以计划的最新版本为准，不冻结开工时的想象。
    """
    out, seen = [], set()

    def _add(text):
        t = _clip(text, 160)
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    plan = plan if isinstance(plan, dict) else {}
    for t in plan.get("done_when") or []:
        _add(t)
    for m in plan.get("milestones") or []:
        if isinstance(m, dict):
            _add(m.get("done_when"))
    for s in plan.get("steps") or []:
        if isinstance(s, dict):
            _add(s.get("verify"))
    return out[:MAX_CHECKS]


def _norm(text):
    return _TEXT_NOISE.sub("", str(text or "")).lower()


def uncovered_checks(checks, criteria):
    """计划条件里没有被报告「逐条提到」的项（报告需照抄文本，允许轻微改写）。"""
    got = [_norm(c.get("criterion") or "") for c in (criteria or [])
           if isinstance(c, dict)]
    out = []
    for chk in (checks or []):
        n = _norm(chk)
        if not n:
            continue
        if not any(n in g or (len(g) >= 4 and g in n) for g in got):
            out.append(chk)
    return out


def build_facts(tool_log=None, plan=None, criteria=None, pending_accept=None,
                missing_files=None, metric=None, metric_min=0, written=0,
                changed_files=None):
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
        "metric": metric,
        "metric_min": metric_min,
        "written": int(written or 0),
        "changed_files": [str(x) for x in (changed_files or []) if str(x).strip()][:20],
        "criteria": [c for c in (criteria or []) if isinstance(c, dict)][:MAX_CRITERIA],
        "plan_checks": plan_checks(plan),
    }


def score_cap(facts, min_score=MIN_SCORE):
    """客观事实 → (分数上限, 封顶原因)。

    这些是「地板」：只要事实成立，模型再高的自评也会被压到及格线以下。
    """
    caps, reasons = [100], []
    f = facts or {}
    if f.get("missing_files"):
        caps.append(40)
        reasons.append("写过的文件在磁盘上找不到：%s" % "、".join(f["missing_files"][:4]))
    if f.get("unresolved"):
        caps.append(60)
        reasons.append("%d 个工具调用失败后没有修复" % len(f["unresolved"]))
    if f.get("plan_pending"):
        caps.append(60)
        reasons.append("计划还有 %d 步没完成" % len(f["plan_pending"]))
    if f.get("accept_pending"):
        caps.append(65)
        reasons.append("验收项未通过：%s" % "、".join(f["accept_pending"][:4]))
    metric = f.get("metric")
    try:
        metric = float(metric) if metric is not None else None
    except (TypeError, ValueError):
        metric = None
    try:
        metric_min = float(f.get("metric_min") or 0)
    except (TypeError, ValueError):
        metric_min = 0
    if metric is not None and metric_min > 0 and metric < metric_min:
        caps.append(65)
        reasons.append("客观指标 %s < 阈值 %s" % (metric, metric_min))
    return min(caps), reasons


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
    try:
        score = int(round(float(obj.get("score"))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
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
        criteria.append({"criterion": _clip(name, 160), "passed": passed,
                         "evidence": _clip(evidence, 300), "note": note})
    gaps = [_clip(g, 200) for g in (obj.get("gaps") or []) if str(g or "").strip()][:5]
    return {
        "verdict": verdict,
        "score": score,
        "criteria": criteria,
        "gaps": gaps,
        "next_action": _clip(obj.get("next_action") or "", 300),
        "valid": bool(criteria) or verdict == "blocked",
    }


def merge_report(report, facts, min_score=MIN_SCORE):
    """模型评分 + 客观封顶 → 最终判定。"""
    rep = dict(report or {})
    cap, reasons = score_cap(facts, min_score)
    # 计划里写明的验收条件必须逐条被核对；漏项等于没验，直接封顶。
    missing = uncovered_checks((facts or {}).get("plan_checks") or [],
                               rep.get("criteria") or [])
    if missing:
        cap = min(cap, 65)
        reasons.append("没有核对计划里的验收条件：%s" % "、".join(missing[:3]))
    score = min(int(rep.get("score") or 0), cap)
    verdict = str(rep.get("verdict") or "fail")
    failed_items = [c for c in (rep.get("criteria") or [])
                    if isinstance(c, dict) and not c.get("passed")]
    if verdict == "pass" and (score < min_score or failed_items):
        verdict = "fail"
    if score < min_score and verdict != "blocked":
        verdict = "fail"
    gaps = list(rep.get("gaps") or [])
    for r in reasons:
        if r not in gaps:
            gaps.append(r)
    rep.update({"score": score, "verdict": verdict, "gaps": gaps[:6],
                "cap": cap, "cap_reasons": reasons,
                "facts": facts or {}, "passed": verdict == "pass"})
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
                answer="", sandbox="", has_tools=True, tools=None):
    """给验收代理的任务书。**不传主模型的分数**，只传事实与待核对的声明。"""
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
        parts.append("【计划里写明的验收条件（必须逐条照抄成 criterion 并核对）】\n"
                     + "\n".join("- " + x for x in facts["plan_checks"]))
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
    if facts.get("metric") is not None:
        obj.append("客观指标：%s（阈值 %s）" % (facts["metric"], facts.get("metric_min") or 0))
    parts.append("【系统核对的客观事实】\n" + ("\n".join(obj) or "（没有发现硬伤）"))
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
    head = "【验收子代理】%s · %d 分（及格 %s）" % (
        {"pass": "通过", "blocked": "无法验证"}.get(rep.get("verdict"), "未通过"),
        int(rep.get("score") or 0), rep.get("min_score") or MIN_SCORE)
    lines = [head]
    failed = [c for c in (rep.get("criteria") or [])
              if isinstance(c, dict) and not c.get("passed")]
    for c in failed[:4]:
        ev = ("  证据：" + c.get("evidence")) if c.get("evidence") else ""
        lines.append("  ✗ " + str(c.get("criterion") or "") + ev)
    gaps = rep.get("gaps") or []
    if gaps:
        lines.append("差距：" + "；".join(str(g) for g in gaps[:max_gaps]))
    if rep.get("next_action"):
        lines.append("下一步：" + str(rep["next_action"]))
    return "\n".join(lines)
