# -*- coding: utf-8 -*-
"""验收契约与计划投影：从 app/api/chat.py 抽出的纯函数。

这些函数只做「结构校验 / 文本投影 / 断言计算」，不碰请求状态、不写会话，
因此可以独立测试。chat.py 仍从这里回导（re-export）：外部 `from app.api.chat
import _acceptance_expect` 之类的旧路径不受影响。

别往这里加需要请求上下文的东西（sse / 会话锁 / 工具执行）；那些留在 chat.py。
"""
import json
import re

# 验收项由模型根据任务自己推导；这里只做结构校验 / 执行 / 状态跟踪，
# 不内置任何领域条目（移动速度、绕序、贴图接缝等都不在这里）。
_ACCEPT_SAFE = {"check_code", "read_file", "search_files", "search_code", "git_diff",
                "render_check", "visual_check", "see_image", "image_compare",
                "image_analyze", "asset_scan", "asset_validate", "mesh_inspect",
                "render_views", "geometry_compare", "scene_assert"}
_ACCEPT_TOOLS = _ACCEPT_SAFE | {"run_command"}

# 模型常写「语义名」而不是工具全名：收敛一次再校验，别为名字写法把整条验收项拒掉。
_TOOL_ALIASES = {
    "read": "read_file", "readfile": "read_file", "cat": "read_file",
    "grep": "search_code", "rg": "search_code", "search_code": "search_code",
    "glob": "search_files", "find": "search_files", "ls": "search_files",
    "search": "search_files",
    "bash": "run_command", "shell": "run_command", "exec": "run_command",
    "run": "run_command",
}


def _norm_tool(tool):
    t = str(tool or "").strip()
    if not t:
        return ""
    return _TOOL_ALIASES.get(t.lower(), t)


def _acceptance_metrics(text):
    """从工具输出里取机器可读指标（视觉 / 素材 / 3D / 多视图 / 场景）。"""
    for line in str(text or "").splitlines():
        for marker in ("__VISUAL_METRICS__", "__ASSET_METRICS__",
                       "__IMAGE_METRICS__", "__MESH_METRICS__",
                       "__GEOMETRY_METRICS__", "__SCENE_METRICS__"):
            if line.startswith(marker):
                try:
                    obj = json.loads(line[len(marker):].strip())
                    if isinstance(obj, dict):
                        return obj
                except ValueError:
                    continue
    return {}


def _acceptance_expect(exp, text):
    """执行 expect 断言，返回 (ok, 说明)。支持文本与数值指标。"""
    exp = exp if isinstance(exp, dict) else {}
    ok, notes = True, []
    if "contains" in exp and str(exp["contains"]) not in str(text):
        ok = False
        notes.append("缺少 %r" % exp["contains"])
    if "not_contains" in exp and str(exp["not_contains"]) in str(text):
        ok = False
        notes.append("出现了 %r" % exp["not_contains"])
    if "regex" in exp:
        try:
            if not re.search(str(exp["regex"]), str(text)):
                ok = False
                notes.append("正则未命中")
        except re.error:
            ok = False
            notes.append("正则非法")
    metrics = _acceptance_metrics(text)
    checks = {
        "fps_min": ("fps", ">="),
        "fps_max": ("fps", "<="),
        "console_errors_max": ("console_errors", "<="),
        "resource_errors_max": ("resource_errors", "<="),
        "draw_calls_max": ("draw_calls", "<="),
        "sharpness_min": ("sharpness", ">="),
        "contrast_min": ("contrast", ">="),
        "blank_score_max": ("blank_score", "<="),
        "ssim_min": ("ssim", ">="),
        "missing_max": ("missing", "<="),
        "unreadable_max": ("unreadable", "<="),
        "duplicates_max": ("duplicates", "<="),
        # image_compare：参考物 vs 产物的还原度指标
        "score_min": ("score", ">="),
        "iou_min": ("iou", ">="),
        "aspect_delta_max": ("aspect_delta", "<="),
        "palette_delta_max": ("palette_delta", "<="),
        "edge_delta_max": ("edge_delta", "<="),
        "fill_delta_max": ("fill_delta", "<="),
        "grid_max": ("grid_max", "<="),
        # mesh_inspect：3D 模型的结构化指标
        "triangles_min": ("triangles", ">="),
        "triangles_max": ("triangles", "<="),
        "vertices_max": ("vertices", "<="),
        "materials_max": ("materials", "<="),
        "textures_max": ("textures", "<="),
        "degenerate_max": ("degenerate", "<="),
        "missing_textures_max": ("missing_textures", "<="),
        "missing_files_max": ("missing_files", "<="),
        "normals_missing_max": ("normals_missing", "<="),
        "uv_missing_max": ("uv_missing", "<="),
        "bbox_x_max": ("bbox_x", "<="),
        "bbox_y_max": ("bbox_y", "<="),
        "bbox_z_max": ("bbox_z", "<="),
        # geometry_compare：多视图对照
        "view_score_min": ("view_score_min", ">="),
        "view_iou_min": ("view_iou", ">="),
        "view_ssim_min": ("view_ssim", ">="),
        "views_failed_max": ("views_failed", "<="),
        # scene_assert：场景事实
        "scene_failed_max": ("failed", "<="),
        "scene_passed_min": ("passed", ">="),
        "scene_objects_min": ("objects", ">="),
        "scene_lights_min": ("lights", ">="),
        "scene_triangles_max": ("triangles", "<="),
    }
    for key, (name, op) in checks.items():
        if key not in exp:
            continue
        if name not in metrics or metrics.get(name) is None:
            ok = False
            notes.append("没有指标 %s" % name)
            continue
        try:
            want = float(exp[key])
            got = float(metrics[name])
        except (TypeError, ValueError):
            ok = False
            notes.append("%s 不是数字" % key)
            continue
        if (op == ">=" and got < want) or (op == "<=" and got > want):
            ok = False
            notes.append("%s=%s 未达 %s %s" % (name, metrics[name], op, exp[key]))
    return ok, "；".join(notes)


def _acceptance_criteria(s):
    contract = s.get("acceptance") if isinstance(s, dict) else None
    if not isinstance(contract, dict):
        return []
    return [c for c in (contract.get("criteria") or []) if isinstance(c, dict)]


def _acceptance_status_text(s):
    criteria = _acceptance_criteria(s)
    if not criteria:
        return "当前没有验收项。"
    passed = sum(1 for c in criteria if c.get("status") == "passed")
    mark = {"passed": "✓", "failed": "✗", "manual": "…", "pending": "·"}
    lines = ["验收项：%d 条（通过 %d，未完成 %d）"
             % (len(criteria), passed, len(criteria) - passed)]
    for c in criteria:
        st = c.get("status") or "pending"
        chk = c.get("check") if isinstance(c.get("check"), dict) else {}
        ev = str(c.get("evidence") or "")
        lines.append("  %s %s [%s] %s — %s(%s)%s" % (
            mark.get(st, "·"), c.get("id"), st, c.get("text"),
            chk.get("tool"), json.dumps(chk.get("args") or {}, ensure_ascii=False,
                                        default=str)[:120],
            ("；证据：" + ev[:120]) if ev else ""))
    lines.append("run 跑安全只读项；run_command 类在主循环执行后 record。")
    return "\n".join(lines)


def _acceptance_norm_criterion(c, i=1):
    """把一条验收项收敛成内部结构；失败返回 (None, 原因)。"""
    if not isinstance(c, dict):
        return None, "第 %d 条不是对象。" % i
    text = str(c.get("text") or "").strip()
    check = c.get("check") if isinstance(c.get("check"), dict) else {}
    tool = _norm_tool(check.get("tool"))
    cargs = check.get("args") if isinstance(check.get("args"), dict) else {}
    if not text or not tool:
        return None, "第 %d 条缺少 text 或 check.tool。" % i
    if tool not in _ACCEPT_TOOLS:
        return None, ("第 %d 条的 check.tool 不支持：%s（可用：%s）"
                      % (i, tool, "、".join(sorted(_ACCEPT_TOOLS))))
    exp = c.get("expect") if isinstance(c.get("expect"), dict) else {}
    if tool in ("read_file", "search_files", "search_code", "git_diff") and not any(
            k in exp for k in ("contains", "not_contains", "regex")):
        return None, ("第 %d 条是读取类检查，expect 至少要给 contains / not_contains / regex，"
                      "否则「能读到」不算验收。" % i)
    cid = str(c.get("id") or "a%d" % i).strip()
    if not cid:
        return None, "第 %d 条的 id 为空。" % i
    return {"id": cid, "text": text[:400],
            "check": {"tool": tool, "args": cargs},
            "expect": exp, "status": "pending", "evidence": ""}, ""


def _acceptance_suggest_msg():
    """多步任务还没定义机检项时的**软提醒**（不阻塞，只提醒一次）。

    以前的硬闸门要求开工前必须定义，结果是「为了过闸随便写一条」；
    现在验收标准允许在执行中随产物演进，收尾还有独立的验收子代理复核。
    """
    return ("（系统·验收提醒）这是多步交付任务，但还没有机检验收项。"
            "可以先用 acceptance(action=\"set\") 补 1–5 条：写清「验证什么」"
            "「用哪个工具/命令检查」「期望结果」；"
            "也可以直接继续 —— 收尾时会有独立的验收子代理按实际产物核对。"
            "如果中途发现验收标准已经过时，用 acceptance(action=\"revise\") 改，"
            "不要硬凑一个没意义的通过。")


def _acceptance_nudge_msg(pending):
    names = "、".join(str(c.get("id") or "?") for c in pending[:5])
    return ("（系统·验收未完成）还有 %d 条未通过/未记录：%s。"
            "先 acceptance(action=\"status\") 查看；安全只读项用 acceptance(action=\"run\")，"
            "run_command 类在主循环跑完后用 acceptance(action=\"record\", id=..., passed=..., "
            "evidence=...)。确实无法机检的项，请在最终回答里说明原因。"
            % (len(pending), names))


def _plan_fingerprint(plan):
    """计划内容快照：用来判断「连续续跑但计划一步没推进」。

    v2 把 revision / 里程碑 / 验收也纳入：只改验收或补一步也算推进。
    """
    steps = (plan or {}).get("steps") or []
    mss = (plan or {}).get("milestones") or []
    try:
        rev = int((plan or {}).get("revision") or 0)
    except (TypeError, ValueError):
        rev = 0
    return (rev,
            tuple((str(m.get("id") or ""), str(m.get("title") or "")) for m in mss
                  if isinstance(m, dict)),
            tuple((str(s.get("status") or ""), str(s.get("text") or ""),
                   str(s.get("verify") or "")) for s in steps if isinstance(s, dict)))


def _latest_plan(s):
    """最后一条 assistant 消息里的计划（跨轮继承用）。

    只看最后一条：以前会一直往前翻，于是「接着补基础」这种弱衔接词能把
    几个任务之前的旧计划装回来 —— 现场 s_2eed2dd969cf idx702，简历任务的
    5 步计划被带进数学辅导，收尾时报「未完成：重写项目经历表述…写 resume.html」。
    """
    for m in reversed((s or {}).get("messages") or []):
        if m.get("role") != "assistant":
            continue
        p = m.get("plan")
        return p if isinstance(p, dict) and p.get("steps") else None
    return None


def _plan_done_milestones(plan):
    """返回 {milestone_id: title}：只有「有步骤且全部 done」的里程碑算完成。"""
    out = {}
    steps = (plan or {}).get("steps") or []
    for m in (plan or {}).get("milestones") or []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "")
        ss = [s for s in steps if isinstance(s, dict) and str(s.get("milestone") or "") == mid]
        if ss and all(str(s.get("status") or "") == "done" for s in ss):
            out[mid] = str(m.get("title") or mid)
    return out


def _plan_accept_gaps(plan, criteria):
    """已标 done 的步骤挂的验收项还没通过（或不存在）→ [(step_id, accept_id, status)]。

    计划 ↔ 验收联动：步骤标 done 只代表「代码改了」，验收项 status=passed
    才代表「真的可用」；两者不一致时要把差距摆给模型，而不是让它跳着往下做。
    只看 done 的步骤 —— 没开始的步骤本来就不该有验收结果。
    """
    by = {str(c.get("id") or ""): c for c in (criteria or []) if isinstance(c, dict)}
    gaps = []
    for s in (plan or {}).get("steps") or []:
        if not isinstance(s, dict) or str(s.get("status") or "") != "done":
            continue
        for aid in (s.get("accept") or []):
            key = str(aid or "")
            if not key:
                continue
            c = by.get(key)
            st = str((c or {}).get("status") or "missing")
            if st != "passed":
                gaps.append((str(s.get("id") or ""), key, st))
    return gaps


def _plan_accept_hint(gaps):
    """把验收差距写成一句可执行的话（最多列 3 条）。"""
    if not gaps:
        return ""
    mark = {"pending": "未跑", "failed": "未通过", "manual": "待记结果",
            "missing": "不存在"}
    bits = []
    for sid, aid, st in gaps[:3]:
        bits.append("%s → %s（%s）" % (sid or "?", aid, mark.get(st, st)))
    more = "；另有 %d 项" % (len(gaps) - 3) if len(gaps) > 3 else ""
    return ("【验收联动】%s。先 acceptance(action=\"run\") 跑一遍，"
            "命令类用 record 记录证据；全部通过再推进下一个里程碑。"
            % ("；".join(bits) + more))


def _plan_result_text(plan):
    """工具结果里的计划清单：带 id / 里程碑 / 验收，方便模型用 plan_patch 定位。"""
    steps = (plan or {}).get("steps") or []
    mss = (plan or {}).get("milestones") or []
    mark = {"done": "[x]", "doing": "[>]", "pending": "[ ]", "blocked": "[!]"}
    lines = []
    goal = str((plan or {}).get("goal") or "").strip()
    if goal:
        lines.append("目标：" + goal)
    dw = (plan or {}).get("done_when") or []
    if dw:
        lines.append("完成判据：" + "；".join(str(x) for x in dw))
    used = set()
    for m in mss:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "")
        lines.append("%s %s%s" % (mid, m.get("title") or "",
                                  ("（验收：%s）" % m.get("done_when")) if m.get("done_when") else ""))
        for s in steps:
            if str(s.get("milestone") or "") != mid:
                continue
            used.add(id(s))
            lines.append("  %s %s %s%s%s%s" % (
                s.get("id") or "", mark.get(s.get("status"), "[ ]"), s.get("text") or "",
                ("  ↳ 验证：" + str(s.get("verify"))) if s.get("verify") else "",
                ("  ↗ " + str(s.get("ref"))) if s.get("ref") else "",
                ("  ⚑ " + ",".join(str(x) for x in (s.get("accept") or [])))
                if s.get("accept") else ""))
    for s in steps:
        if id(s) in used:
            continue
        lines.append("%s %s %s%s%s%s" % (
            s.get("id") or "", mark.get(s.get("status"), "[ ]"), s.get("text") or "",
            ("  ↳ 验证：" + str(s.get("verify"))) if s.get("verify") else "",
            ("  ↗ " + str(s.get("ref"))) if s.get("ref") else "",
            ("  ⚑ " + ",".join(str(x) for x in (s.get("accept") or [])))
            if s.get("accept") else ""))
    retro = str((plan or {}).get("retro") or "").strip()
    if retro:
        lines.append("复盘：" + retro)
    return "\n".join(lines)


def _plan_sse(plan):
    """计划卡事件：把 v2 字段一起发过去，旧前端只读 steps 也不会坏。"""
    return {"type": "plan",
            "steps": plan.get("steps") or [],
            "milestones": plan.get("milestones") or [],
            "goal": plan.get("goal") or "",
            "done_when": plan.get("done_when") or [],
            "revision": plan.get("revision") or 1,
            "log": plan.get("log") or {},
            "retro": plan.get("retro") or "",
            "note": plan.get("note") or ""}
