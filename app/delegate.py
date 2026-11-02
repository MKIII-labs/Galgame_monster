# -*- coding: utf-8 -*-
"""delegate：把任务派给可配置的只读子代理团队。

从 chat 主循环里抽出来，便于单测：这里只做
「解析定义 -> explore.run / start_background -> 拼回灌文本」。
工具执行、审批、搜索、事件流都由调用方通过 ctx 注入。
"""
from app import agent_defs, explore


def resolve(raw, limit=12):
    """[{agent, task}] -> (roles, questions, problems)。

    limit 由调用方传入（默认取 explore 的硬上限 12）；运行时通常来自
    agent_explore_max（默认 8）。
    """
    roles, questions, bad = {}, [], []
    items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("agent") or "").strip()
        task = str(item.get("task") or "").strip()
        if not aid or not task:
            continue
        try:
            spec = agent_defs.get_agent(aid)
        except Exception as e:                   # noqa: BLE001  单个定义坏了不影响其他
            bad.append("%s：%s" % (aid, e))
            continue
        if not spec:
            bad.append("未知或已停用：%s" % aid)
            continue
        roles[aid] = agent_defs.role_spec(spec)
        questions.append({"agent": aid, "task": task})
    return roles, questions, bad


def _limit(ctx):
    """单次最多派几个：配置 agent_explore_max，硬上限 12。"""
    try:
        n = int((ctx or {}).get("max_agents") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = 12
    return max(1, min(n, 12))


def _stage_text(res):
    rows = res.get("results") or []
    if rows and rows[0].get("text"):
        return str(rows[0].get("text") or "")
    return explore.format_results(res)


def _run_pipeline(ids, task, ctx):
    """串行交接：上一阶段的结论作为下一阶段的背景。"""
    outs, prior, batch = [], "", ""
    ok_all, tokens = True, 0
    for i, aid in enumerate(ids[:6]):
        roles, questions, bad = resolve([{"agent": aid,
                                          "task": task if i == 0 else
                                          "%s\n\n上一步输出：\n%s" % (task, prior[:2000])}])
        if not questions:
            outs.append("### %d. %s\n失败：%s" % (i + 1, aid, "；".join(bad) or "无法解析"))
            ok_all = False
            break
        c = dict(ctx or {})
        c["roles"] = roles
        c["allow_write"] = False
        if prior:
            c["parent_brief"] = prior[:4000]
        res = explore.run({"goal": task, "agents": questions}, c)
        text = _stage_text(res)
        outs.append("### %d. %s\n%s" % (i + 1, aid, text or "（没有输出）"))
        prior = text or prior
        batch = str(res.get("batch") or batch)
        tokens += int(res.get("tokens") or 0)
        if not res.get("ok"):
            ok_all = False
            break
    return "\n\n".join(outs), batch, ok_all, tokens


def run(args, ctx):
    args = args if isinstance(args, dict) else {}
    pipeline = args.get("pipeline")
    if isinstance(pipeline, list) and pipeline:
        if bool(args.get("background")):
            return {"ok": False, "text": "", "batch": "", "agents": [],
                    "bad": [], "tokens": 0,
                    "error": "pipeline 暂不支持后台运行"}
        ids = [str(x or "").strip() for x in pipeline if str(x or "").strip()]
        task = str(args.get("task") or args.get("goal") or "").strip()
        if not ids or not task:
            return {"ok": False, "text": "", "batch": "", "agents": [],
                    "bad": [], "tokens": 0,
                    "error": "pipeline 需要 pipeline=[agent...] 和 task"}
        text, batch, ok_all, tokens = _run_pipeline(ids, task, ctx)
        if batch:
            text += "\n\n批次号 %s（可用 explore_followup 追问最后一阶段）" % batch
        return {"ok": ok_all, "text": text, "batch": batch,
                "agents": [{"agent": a, "task": task} for a in ids],
                "bad": [], "tokens": tokens}
    roles, questions, bad = resolve(args.get("agents"),
                                    _limit(ctx))
    if not questions:
        return {"ok": False, "text": "", "batch": "", "agents": [], "bad": bad,
                "tokens": 0,
                "error": "没有可用的子代理。" + ("；".join(bad) if bad else "")}
    c = dict(ctx or {})
    c["roles"] = roles
    c["allow_write"] = False
    payload = {
        "goal": args.get("goal") or "",
        "agents": questions,
        "deliverable": args.get("deliverable") or "",
        "verify": bool(args.get("verify")),
        "acceptance": args.get("acceptance"),
    }
    if bool(args.get("background")):
        started = explore.start_background(payload, c)
        if not started.get("ok"):
            return {"ok": False, "text": "", "batch": "", "agents": questions,
                    "bad": bad, "tokens": 0,
                    "error": started.get("error") or "后台启动失败"}
        text = ("【delegate】已在后台启动批次 %s（%d 个代理，只读）。"
                "本回合继续做别的；用 explore_status 看进度，"
                "explore_result 取结果，explore_cancel 取消。"
                % (started["batch"], len(questions)))
        return {"ok": True, "text": text, "batch": started["batch"],
                "agents": questions, "bad": bad, "tokens": 0}
    res = explore.run(payload, c)
    text = explore.format_results(res)
    # 可写代理并行时提醒一句：写入串行执行，同文件冲突会被标记（strict 下会拒绝）
    try:
        writers = [q["agent"] for q in questions
                   if (roles.get(q["agent"]) or {}).get("allow_write")]
    except Exception:                    # noqa: BLE001
        writers = []
    if len(writers) > 1:
        text += ("\n\n（写隔离：本批有 %d 个可写代理（%s），写入串行执行；"
                 "同一文件被多个代理修改会标记冲突，strict 模式下第二次写入会被拒绝。）"
                 % (len(writers), "、".join(writers[:6])))
    batch = str(res.get("batch") or "")
    if batch:
        text += "\n\n批次号 %s（可用 explore_followup 在同一批子代理上追问）" % batch
    return {"ok": bool(res.get("ok")), "text": text, "batch": batch,
            "agents": questions, "bad": bad,
            "tokens": int(res.get("tokens") or 0), "result": res,
            "error": res.get("error") or ""}
