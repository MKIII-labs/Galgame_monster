# -*- coding: utf-8 -*-
"""自动派发：suggest / auto-plan / auto-review 三档。

三档共用「只读子代理」这条边界：
- suggest：在系统提示里给出团队清单与派活时机；连续读很多次还没派活时提醒一次。
- auto-plan：回合开始先跑 planner，把计划注入主上下文（不动手、不写文件）。
- auto-review：本轮有写入时，finish 之前派 reviewer 复核，结论回灌给主代理。

写入仍然只能由主代理显式调用 delegate（implementer）并走审批；
这里不做任何写操作，也不替模型执行工具。
"""
import re

from app import agent_defs, delegate

# 只读 / 检索类工具：连续读了很多次还没派活 → 说明该并行了
READ_TOOLS = {
    "read_file", "read_files", "list_dir", "search_files", "search_code",
    "repo_map", "code_context", "impact", "find_symbol",
    "lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_hover",
    "lsp_symbols", "lsp_implementation", "lsp_calls", "check_code",
    "git_status", "git_diff", "git_log", "web_search", "fetch_url",
    "read_tool_result",
}
# 已经派过活（或跑过多代理编排）就不必再提醒
DELEGATE_TOOLS = {"delegate", "explore", "explore_followup", "workflow_run"}
# 连续这么多次只读还没派活 → 提醒一次
READ_NUDGE_AT = 4

# 像「要动手做一件事」的说法；纯问答 / 寒暄不触发自动规划
_TASK_RE = re.compile(
    r"实现|重构|重写|修复|排查|优化|改造|升级|接入|迁移|新增|添加|删除|拆分|合并"
    r"|整理|清理|设计|编写|写一个|写个|做个|做一个|搭建|部署|跑测试|测试|评审|审查"
    r"|检查|分析|还原|复刻|复现|调研|研究|规划|计划|方案|补上|补全|替换|支持")
_CONT_RE = re.compile(r"^(继续|接着|然后|往下|下一步|刚才|刚说)|继续做|接着做")


def enabled_agents():
    """当前启用且定义没坏的子代理。"""
    out = []
    for a in agent_defs.list_agents():
        if a.get("enabled") and not a.get("error"):
            out.append(a)
    return out


def routing_block():
    """系统提示里的「什么时候该派活」块；没有可用代理就返回空串。"""
    rows = enabled_agents()
    if not rows:
        return ""
    lines = ["【自动派发（suggest）】可用子代理（delegate 的 agent 参数）："]
    for a in rows:
        perm = "可写" if str(a.get("permission")) == "write" else "只读"
        desc = str(a.get("description") or "").strip()
        lines.append("- %s（%s）%s" % (a.get("id"), perm, ("：" + desc) if desc else ""))
    lines.append(
        "派活时机：需要同时查 3 个以上互不相关的问题 / 模块（并行派多个 agents）；"
        "需要独立复核已完成的改动（reviewer）或挑刺（critic）；"
        "任务明显多阶段、适合串行交接（pipeline）。")
    lines.append(
        "不该派活：单文件小改、纯问答、已有结论只差一句话。"
        "派活只是把「读」并行出去——写操作仍由你自己执行；"
        "implementer 必须显式派发，且每次写入都会走审批。")
    return "\n".join(lines)


def plan_worthy(text):
    """这条用户消息值不值得先跑一次 planner。"""
    t = str(text or "").strip()
    if len(t) < 8:
        return False
    if _CONT_RE.search(t):
        return False          # 继续 / 接着：沿用上一轮计划，不重复规划
    if t.startswith("?"):
        return False
    return bool(_TASK_RE.search(t))


def _read_streak(tool_log):
    """从后往前数：最近一次写入之后连续读了几次；中途派过活就返回 0。"""
    n = 0
    for e in reversed(list(tool_log or [])):
        name = str(e.get("tool") or "")
        if name in DELEGATE_TOOLS:
            return 0
        if e.get("write"):
            return n
        if name in READ_TOOLS:
            n += 1
    return n


def route_hint(tool_log):
    """连续只读太多次 → 给主代理一段一次性提醒；否则空串。"""
    if _read_streak(tool_log) < READ_NUDGE_AT:
        return ""
    return ("【自动派发建议】这一轮已经连续做了多次只读调用，还没有派活。"
            "如果这些调查彼此独立，改用 delegate 并行派给 researcher / planner，"
            "一次拿回几份结论；已经接近收尾就忽略这条。")


def plan_prompt(goal):
    """planner 的任务描述：只要计划，不要动手。"""
    return ("为用户请求做执行前规划。只读，不要改文件、不要执行命令。\n"
            "用户请求：\n%s\n\n"
            "请给出 3–6 步可执行计划：每步写做什么、改哪些文件、怎么验证；"
            "标出风险和需要用户拍板的点。不要复述用户请求。" % str(goal or "")[:2000])


def plan_note(text):
    """把 planner 的结论包成注入主上下文的提示。"""
    t = str(text or "").strip()
    if not t:
        return ""
    return ("【自动规划（planner 子代理，只读）】建议按下面的步骤执行：\n"
            + t[:1600]
            + "\n\n若认同，第一轮先用 update_plan 落成计划卡（可调整），不要整段复述；"
              "不认同就按自己的判断做。")


def review_prompt(goal, summary, tool_log):
    """reviewer 的任务描述：复核本轮写入，只读。"""
    writes = []
    for e in (tool_log or []):
        if not e.get("write"):
            continue
        name = str(e.get("tool") or "")
        args = e.get("args") or {}
        path = str(args.get("path") or args.get("file") or "").strip()
        writes.append("- %s %s" % (name, path))
        if len(writes) >= 12:
            break
    parts = ["用户请求：\n%s" % str(goal or "")[:1500]]
    if str(summary or "").strip():
        parts.append("主代理声明的交付：\n%s" % str(summary)[:600])
    if writes:
        parts.append("本轮执行的写操作：\n" + "\n".join(writes))
    parts.append("请只读复核这些改动：正确性、遗漏、回归风险与明显缺陷。"
                 "按严重程度列出问题（最多 5 条，带文件:行号），最后一行写结论："
                 "可交付 / 需修复。")
    return "\n\n".join(parts)


def review_note(text):
    """把 reviewer 的结论包成回灌给主代理的提示。"""
    t = str(text or "").strip()
    if not t:
        return ""
    return ("【自动复核（reviewer 子代理，只读）】\n" + t[:1500]
            + "\n\n如果结论是「需修复」，先修完再收尾；只是建议就说明取舍。")


def run_agent(agent_id, task, ctx):
    """跑单个命名代理，返回 delegate.run 的结果。"""
    return delegate.run({"agents": [{"agent": agent_id, "task": task}]}, ctx)


def first_text(res):
    """取单个代理的结论正文；失败返回空串。"""
    if not isinstance(res, dict):
        return ""
    inner = res.get("result") or res
    rows = inner.get("results") if isinstance(inner, dict) else None
    if rows:
        return str(rows[0].get("text") or "").strip()
    return ""
