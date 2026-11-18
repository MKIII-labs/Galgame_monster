# -*- coding: utf-8 -*-
"""工作台 API：任务 / 产物 / 场景（由 app.registry 自注册）"""
import base64
import datetime as _dt
import json
import mimetypes
import os
import re

from app import agent, calendar, capabilities, llm, mem_tool, scenes, task_engine, work_extras
from app.config import get_api_conf
from app.registry import route
from app.sessions import find_session, load_sessions, save_sessions
from app.store import DATA_DIR

_TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".log",
             ".py", ".js", ".ts", ".css", ".html", ".htm", ".xml", ".yaml",
             ".yml", ".ini", ".toml", ".bat", ".ps1", ".sh", ".sql"}
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_PREVIEW_MAX = 256 * 1024
_IMAGE_MAX = 5 * 1024 * 1024


def _bad(h, msg, code=400):
    return h._json({"ok": False, "error": msg}, code)


def _blocked_by(tid):
    """依赖未完成时返回前几个依赖标题，否则返回空列表。"""
    task = task_engine.get_task(tid)
    if not task:
        return []
    out = []
    for dep in str(task.get("depends_on") or "").split(","):
        dep = dep.strip()
        if not dep:
            continue
        other = task_engine.get_task(dep)
        if other and other.get("status") != "done":
            out.append(other.get("title") or dep)
        if len(out) >= 3:
            break
    return out


# ---------------------------------------------------------------- AI 工作助手
# 只做「建议」：计划 / 任务拆解 / 复盘都返回结构化结果，写不写由前端等用户确认。
# 模型不可用时返回本地规则兜底（ai=false），保证工作台基础功能不受影响。

_OPEN_STATES = ("pending", "running", "paused", "failed")


def _day_range(days=1):
    start = _dt.datetime.combine(_dt.date.today(), _dt.time.min).timestamp()
    return start, start + max(1, int(days)) * 86400


def _fmt_ts(ts, fmt="%m-%d %H:%M"):
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime(fmt)
    except (TypeError, ValueError, OSError):
        return ""


def _assist_json(prompt, max_tokens=900):
    """调用当前聊天模型并解析 JSON。返回 (data, error)。"""
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return None, "未配置聊天 API 密钥"
    try:
        raw = llm._quick_chat(
            api_key, api_url, api_model,
            [{"role": "system", "content": "你是个人工作助理。只输出 JSON，不要解释，不要 Markdown 围栏。"},
             {"role": "user", "content": prompt}],
            max_tokens=max_tokens, source="work", temperature=0.3, use_cache=False) or ""
    except Exception as e:                       # noqa: BLE001  模型失败要能降级
        return None, "调用失败：%s" % e
    data = mem_tool._loads_loose(raw)
    if not isinstance(data, dict):
        return None, "模型没有返回 JSON"
    return data, ""


def _assist_text(prompt, max_tokens=900):
    """调用聊天模型取纯文本（复盘这种本来就要求 Markdown 的场景）。"""
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return "", "未配置聊天 API 密钥"
    try:
        txt = llm._quick_chat(
            api_key, api_url, api_model,
            [{"role": "system", "content": "你是个人工作助理，回答简洁、只依据用户给的数据。"},
             {"role": "user", "content": prompt}],
            max_tokens=max_tokens, source="work", temperature=0.4, use_cache=False) or ""
        return txt.strip(), ""
    except Exception as e:                       # noqa: BLE001
        return "", "调用失败：%s" % e


def _plan_fallback(tasks):
    """本地兜底：按优先级 / 截止时间挑 3 个最该推进的任务。"""
    def key(t):
        due = float(t.get("due_at") or 0)
        return (-int(t.get("priority") or 0), due if due else 9e18,
                -float(t.get("updated_at") or 0))
    top = sorted([t for t in tasks if t.get("status") in _OPEN_STATES], key=key)[:3]
    return {
        "focus": [{"id": t.get("id"), "why": "按优先级与截止时间排在最前"} for t in top],
        "blocks": [],
        "note": "先用本地规则列出今天最值得推进的任务；配置聊天模型后可生成时间块。",
    }


def _assist_plan():
    tasks = task_engine.list_tasks(limit=120)
    start, end = _day_range(1)
    events = calendar.list_events(start, end)
    ev_lines = []
    for e in events[:20]:
        t0 = _fmt_ts(e.get("occurrence_at"), "%H:%M")
        t1 = _fmt_ts(e.get("end_at"), "%H:%M")
        ev_lines.append("%s-%s %s" % (t0, t1, e.get("title") or ""))
    task_lines = []
    for t in tasks[:100]:
        prog = t.get("progress") or {}
        task_lines.append("%s | %s | %s | P%s | %s | %s/%s" % (
            t.get("id"), (t.get("title") or "")[:60], t.get("status") or "pending",
            int(t.get("priority") or 0),
            _fmt_ts(t.get("due_at"), "%m-%d %H:%M") or "无截止",
            int(prog.get("done") or 0), int(prog.get("total") or 0)))
    weekday = "一二三四五六日"[_dt.date.today().weekday()]
    prompt = (
        "今天是 %s（周%s）。\n\n今天的日程：\n%s\n\n未完成任务（id | 标题 | 状态 | 优先级 | 截止 | 步骤进度）：\n%s\n\n"
        "请给出今日计划，只输出这个 JSON：\n"
        '{"focus":[{"id":"任务id","why":"一句话理由"}],'
        '"blocks":[{"start":"09:30","end":"10:30","title":"做什么","task_id":"任务id或空"}],'
        '"note":"一句提醒"}\n'
        "要求：focus 1-3 个，只能从上面的任务 id 里选；blocks 0-4 个，时间在 08:00-22:00 之间、"
        "start < end、避开已有日程；没有合适内容时用空数组，不要编造任务 id。"
    ) % (_dt.date.today().strftime("%Y-%m-%d"), weekday,
         "\n".join(ev_lines) or "（无）", "\n".join(task_lines) or "（无）")
    data, err = _assist_json(prompt, max_tokens=900)
    fallback = _plan_fallback(tasks)
    if not data:
        fallback["note"] = (err + "；" + fallback["note"]) if err else fallback["note"]
        return {"ok": True, "ai": False, "plan": fallback, "message": err}
    by_id = {t.get("id"): t for t in tasks}
    focus = []
    for item in (data.get("focus") if isinstance(data.get("focus"), list) else [])[:3]:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip()
        if tid not in by_id:
            continue
        focus.append({"id": tid, "why": str(item.get("why") or "").strip()[:80]})
    blocks = []
    for item in (data.get("blocks") if isinstance(data.get("blocks"), list) else [])[:4]:
        if not isinstance(item, dict):
            continue
        m0 = re.match(r"^(\d{1,2}):(\d{2})$", str(item.get("start") or "").strip())
        m1 = re.match(r"^(\d{1,2}):(\d{2})$", str(item.get("end") or "").strip())
        if not m0 or not m1:
            continue
        a = int(m0.group(1)) * 60 + int(m0.group(2))
        b = int(m1.group(1)) * 60 + int(m1.group(2))
        if not (8 * 60 <= a < b <= 22 * 60):
            continue
        tid = str(item.get("task_id") or "").strip()
        blocks.append({"start": m0.group(0).zfill(5) if len(m0.group(0)) == 4 else m0.group(0),
                       "end": m1.group(0).zfill(5) if len(m1.group(0)) == 4 else m1.group(0),
                       "title": str(item.get("title") or "专注工作").strip()[:60],
                       "task_id": tid if tid in by_id else ""})
    plan = {"focus": focus, "blocks": blocks,
            "note": str(data.get("note") or "").strip()[:120]}
    if not focus and not blocks:
        plan = fallback
        return {"ok": True, "ai": True, "plan": plan, "message": "模型没有给出可用计划"}
    return {"ok": True, "ai": True, "plan": plan}


def _assist_breakdown(body):
    title = str(body.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "缺少任务标题"}
    intent = str(body.get("intent") or "").strip()[:800]
    existing = []
    tid = str(body.get("task_id") or "").strip()
    if tid:
        task = task_engine.get_task(tid)
        if task:
            existing = [str(s.get("title") or "") for s in (task.get("steps") or [])
                        if s.get("title")]
    prompt = (
        "任务：%s\n补充说明：%s\n已有步骤：%s\n\n"
        "请把它拆成 2-6 个可执行步骤，只输出 JSON：\n"
        '{"steps":["步骤1","步骤2"],"note":"一句提醒"}\n'
        "要求：每个步骤不超过 40 字、动词开头、能在一轮内完成；不要重复已有步骤。"
    ) % (title, intent or "（无）", "、".join(existing) or "（无）")
    data, err = _assist_json(prompt, max_tokens=600)
    steps = []
    if data:
        raw = data.get("steps")
        if isinstance(raw, list):
            for s in raw[:8]:
                s = str(s or "").strip()
                if s:
                    steps.append(s[:60])
    if not steps:
        # 本地兜底：按行拆分补充说明，再不行给一个通用三段式
        steps = [x.strip()[:60] for x in re.split(r"[\n；;]", intent) if x.strip()][:6]
        if not steps:
            steps = ["梳理目标与所需材料", "完成主要工作", "检查结果并交付"]
        return {"ok": True, "ai": False, "steps": steps,
                "note": (err + "；" if err else "") + "这是本地兜底步骤，可继续编辑。"}
    return {"ok": True, "ai": True, "steps": steps,
            "note": str(data.get("note") or "").strip()[:120]}


def _assist_review(body):
    days = max(1, min(30, int(body.get("days") or 7)))
    stats = work_extras.summary(days)
    done_tasks = [t for t in task_engine.list_tasks(status="done", limit=200)
                  if float(t.get("finished_at") or 0) >= float(stats.get("start_at") or 0)]
    done_titles = [t.get("title") or "" for t in done_tasks[:12] if t.get("title")]
    status_count = stats.get("status_count") or {}
    prompt = (
        "近 %s 天工作数据：\n"
        "- 新建任务 %s 个，完成 %s 个，逾期 %s 个\n"
        "- 专注 %s 分钟 / %s 段\n"
        "- 状态分布：%s\n"
        "- 高频标签：%s\n"
        "- 最近完成：%s\n\n"
        "请写一份简洁的工作复盘，Markdown，300-450 字，分「进展 / 卡点 / 下一步」三节；"
        "只依据上面的数据，不要编造。"
    ) % (days, sum(stats.get("created_by_day") or []), len(done_tasks),
         stats.get("overdue") or 0, stats.get("focus_minutes") or 0,
         stats.get("focus_sessions") or 0,
         "、".join("%s %s" % (k, v) for k, v in status_count.items()) or "无",
         "、".join("%s×%s" % (k, v) for k, v in (stats.get("tag_count") or [])) or "无",
         "、".join(done_titles) or "无")
    data, err = _assist_json(prompt, max_tokens=900)
    text = str((data or {}).get("markdown") or "").strip() if data else ""
    if not text:
        text, err2 = _assist_text(prompt, max_tokens=900)
        if text:
            return {"ok": True, "ai": True, "markdown": text}
        err = err2 or err
    if not text:
        text = ("## 近 %s 天复盘\n\n"
                "### 进展\n- 新建任务 %s 个，完成 %s 个。\n- 专注 %s 分钟 / %s 段。\n\n"
                "### 卡点\n- 当前逾期 %s 个任务。\n\n"
                "### 下一步\n- 先处理逾期任务，再按优先级推进进行中的工作。\n"
                % (days, sum(stats.get("created_by_day") or []), len(done_tasks),
                   stats.get("focus_minutes") or 0, stats.get("focus_sessions") or 0,
                   stats.get("overdue") or 0))
        return {"ok": True, "ai": False, "markdown": text,
                "message": err or "模型不可用，已生成本地摘要"}
    return {"ok": True, "ai": True, "markdown": text}


@route("/api/scene")
def _api_scene(h):
    body = h._body()
    op = str(body.get("op") or "get").strip()
    data = load_sessions()
    sid = str(body.get("session_id") or data.get("current") or "")
    s = find_session(data, sid)
    if not s:
        return _bad(h, "会话不存在", 404)
    if op == "get":
        return h._json({"ok": True, "scene": s.get("scene") or "general",
                        "scenes": scenes.list_scenes()})
    if op == "set":
        scene = scenes.normalize(body.get("scene"))
        s["scene"] = scene
        save_sessions(data)
        return h._json({"ok": True, "scene": scene,
                        "scenes": scenes.list_scenes()})
    return _bad(h, "未知操作：%s" % op)


@route("/api/work/quick_add")
def _api_work_quick_add(h):
    """工作台快速添加：一句话 → 一条日程。"""
    body = h._body()
    try:
        ev = calendar.create_quick_event(body.get("text"))
    except ValueError as e:
        return _bad(h, str(e))
    return h._json({"ok": True, "event": ev})


@route("/api/work/caps")
def _api_work_caps(h):
    """任务步骤可绑定的能力清单（供「编辑计划」下拉选择）。"""
    items = []
    for cid in capabilities.ids():
        cap = capabilities.get(cid)
        if cap is None:
            continue
        items.append({"id": cap.id, "desc": cap.description,
                      "perm": cap.permission})
    return h._json({"ok": True, "items": items})


@route("/api/work/assist")
def _api_work_assist(h):
    """AI 工作助手：只返回建议，写入动作仍由用户在界面上确认。"""
    body = h._body()
    op = str(body.get("op") or "plan_today").strip()
    if op == "plan_today":
        return h._json(_assist_plan())
    if op == "breakdown":
        return h._json(_assist_breakdown(body))
    if op == "review":
        return h._json(_assist_review(body))
    return _bad(h, "未知操作：%s" % op)


@route("/api/work/tasks")
def _api_work_tasks(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()

    if op == "list":
        items = task_engine.list_tasks(
            status=body.get("status") or "",
            limit=body.get("limit") or 200,
            tag=body.get("tag") or "",
            priority=body.get("priority"),
            parent_id=body.get("parent_id"))
        return h._json({"ok": True, "tasks": items})

    if op == "get":
        task = task_engine.get_task(body.get("id"))
        return h._json({"ok": bool(task), "task": task} if task else
                       {"ok": False, "error": "找不到任务"})

    if op == "create":
        payload = dict(body)
        tpl_id = str(body.get("template_id") or "").strip()
        if tpl_id:
            tpl = work_extras.get_template(tpl_id)
            if not tpl:
                return _bad(h, "模板不存在")
            payload.setdefault("title", tpl["title"])
            payload.setdefault("steps", tpl["steps"])
            payload.setdefault("tags", tpl["tags"])
            payload.setdefault("priority", tpl["priority"])
            payload.setdefault("estimate_min", tpl["estimate_min"])
        try:
            task = task_engine.create_task(
                payload.get("title"), payload.get("steps") or [],
                session_id=payload.get("session_id") or "",
                scene=payload.get("scene") or "general",
                intent=payload.get("intent") or "",
                due_at=payload.get("due_at"),
                remind_before=payload.get("remind_before"),
                priority=payload.get("priority") or 0,
                tags=payload.get("tags") or "",
                parent_id=payload.get("parent_id") or "",
                depends_on=payload.get("depends_on") or "",
                repeat_rule=payload.get("repeat_rule") or "",
                estimate_min=payload.get("estimate_min") or 0)
        except ValueError as e:
            return _bad(h, str(e))
        if body.get("auto_start"):
            task = task_engine.start_task(task["id"])
        return h._json({"ok": True, "task": task})

    if op == "update":
        tid = str(body.get("id") or "").strip()
        if not tid:
            return _bad(h, "缺少 id")
        try:
            task = task_engine.update_task(
                tid, title=body.get("title"), priority=body.get("priority"),
                tags=body.get("tags"),
                due_at=body.get("due_at") if "due_at" in body else None,
                remind_before=body.get("remind_before"),
                parent_id=body.get("parent_id"),
                depends_on=body.get("depends_on"),
                repeat_rule=body.get("repeat_rule"),
                estimate_min=body.get("estimate_min"),
                sort_order=body.get("sort_order"))
        except (TypeError, ValueError) as e:
            return _bad(h, str(e))
        return h._json({"ok": bool(task), "task": task})

    if op == "complete":
        tid = str(body.get("id") or "").strip()
        if not tid:
            return _bad(h, "缺少 id")
        res = task_engine.complete_task(tid)
        if not res:
            return _bad(h, "找不到任务")
        return h._json({"ok": True, "task": res.get("task"),
                        "next": res.get("next")})

    if op == "batch":
        ids = [str(x) for x in (body.get("ids") or []) if str(x).strip()]
        action = str(body.get("action") or "").strip()
        if not ids or not action:
            return _bad(h, "缺少 ids 或 action")
        done, failed = [], []
        for tid in ids[:200]:
            try:
                if action == "delete":
                    ok = task_engine.delete_task(tid)
                elif action == "complete":
                    ok = bool(task_engine.complete_task(tid))
                elif action in ("start", "pause", "resume", "cancel"):
                    if action in ("start", "resume") and _blocked_by(tid):
                        failed.append(tid)
                        continue
                    fn = {"start": task_engine.start_task,
                          "pause": task_engine.pause_task,
                          "resume": task_engine.resume_task,
                          "cancel": task_engine.cancel_task}[action]
                    ok = bool(fn(tid))
                elif action in ("priority", "tags"):
                    val = body.get("value")
                    ok = bool(task_engine.update_task(tid, **{action: val}))
                else:
                    return _bad(h, "未知批量操作：%s" % action)
                (done if ok else failed).append(tid)
            except (TypeError, ValueError) as e:
                failed.append(tid)
                print("[工作台] 批量操作失败 %s：%s" % (tid, e))
        return h._json({"ok": True, "done": done, "failed": failed})

    if op == "reorder":
        ids = [str(x) for x in (body.get("ids") or []) if str(x).strip()]
        if not ids:
            return _bad(h, "缺少 ids")
        task_engine.reorder_tasks(ids)
        return h._json({"ok": True})

    if op == "retry":
        tid = str(body.get("id") or "").strip()
        if not tid:
            return _bad(h, "缺少 id")
        return h._json({"ok": True, "task": task_engine.retry_task(tid)})

    if op == "add_step":
        tid = str(body.get("id") or "").strip()
        if not tid:
            return _bad(h, "缺少 id")
        args = body.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                return _bad(h, "步骤参数不是合法 JSON")
        try:
            task = task_engine.add_step(
                tid, body.get("title"), tool=body.get("tool") or "",
                args=args or {}, position=body.get("position"))
        except (TypeError, ValueError) as e:
            return _bad(h, str(e))
        return h._json({"ok": True, "task": task})

    if op in ("update_step", "delete_step", "move_step"):
        tid = str(body.get("id") or "").strip()
        sid = str(body.get("step_id") or "").strip()
        if not tid or not sid:
            return _bad(h, "缺少 id 或 step_id")
        try:
            if op == "delete_step":
                task = task_engine.delete_step(tid, sid)
            elif op == "move_step":
                task = task_engine.move_step(tid, sid, body.get("delta") or 0)
            else:
                args = body.get("args")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except ValueError:
                        return _bad(h, "步骤参数不是合法 JSON")
                task = task_engine.update_step(
                    tid, sid, title=body.get("title"), tool=body.get("tool"),
                    args=args)
        except (TypeError, ValueError) as e:
            return _bad(h, str(e))
        return h._json({"ok": True, "task": task})

    if op in ("start", "pause", "resume", "cancel", "delete"):
        tid = str(body.get("id") or "").strip()
        if not tid:
            return _bad(h, "缺少 id")
        if op == "delete":
            return h._json({"ok": task_engine.delete_task(tid)})
        if op in ("start", "resume"):
            blocked = _blocked_by(tid)
            if blocked:
                return _bad(h, "前置任务未完成：%s" % blocked[0])
        fn = {"start": task_engine.start_task,
              "pause": task_engine.pause_task,
              "resume": task_engine.resume_task,
              "cancel": task_engine.cancel_task}[op]
        task = fn(tid)
        return h._json({"ok": bool(task), "task": task})

    if op == "confirm_step":
        tid = str(body.get("id") or "").strip()
        sid = str(body.get("step_id") or "").strip()
        if not tid or not sid:
            return _bad(h, "缺少 id 或 step_id")
        task = task_engine.confirm_step(tid, sid)
        return h._json({"ok": bool(task), "task": task})

    if op == "complete_step":
        tid = str(body.get("id") or "").strip()
        sid = str(body.get("step_id") or "").strip()
        if not tid or not sid:
            return _bad(h, "缺少 id 或 step_id")
        task = task_engine.complete_step(tid, sid)
        return h._json({"ok": bool(task), "task": task})

    return _bad(h, "未知操作：%s" % op)


@route("/api/work/templates")
def _api_work_templates(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()
    if op == "list":
        return h._json({"ok": True, "templates": work_extras.list_templates()})
    if op == "save":
        try:
            tpl = work_extras.save_template(
                body.get("name"), body.get("title"), body.get("steps") or [],
                tags=body.get("tags") or "", priority=body.get("priority") or 0,
                estimate_min=body.get("estimate_min") or 0,
                tid=body.get("id") or "")
        except ValueError as e:
            return _bad(h, str(e))
        return h._json({"ok": True, "template": tpl})
    if op == "delete":
        return h._json({"ok": work_extras.delete_template(body.get("id"))})
    return _bad(h, "未知操作：%s" % op)


@route("/api/work/focus")
def _api_work_focus(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()
    if op == "list":
        return h._json({"ok": True, "sessions": work_extras.list_focus(
            start_at=body.get("start_at") or 0,
            end_at=body.get("end_at") or 0,
            limit=body.get("limit") or 200)})
    if op == "add":
        try:
            item = work_extras.add_focus(
                task_id=body.get("task_id") or "", title=body.get("title") or "",
                start_at=body.get("start_at") or 0, end_at=body.get("end_at") or 0,
                minutes=body.get("minutes") or 0, kind=body.get("kind") or "focus")
        except ValueError as e:
            return _bad(h, str(e))
        return h._json({"ok": True, "session": item})
    return _bad(h, "未知操作：%s" % op)


@route("/api/work/stats")
def _api_work_stats(h):
    body = h._body()
    return h._json({"ok": True, "stats": work_extras.summary(body.get("days") or 7)})


@route("/api/work/artifacts")
def _api_work_artifacts(h):
    body = h._body()
    op = str(body.get("op") or "list").strip()
    if op == "list":
        return h._json({"ok": True, "artifacts": task_engine.list_artifacts(
            task_id=body.get("task_id") or "",
            limit=body.get("limit") or 50,
            include_working=bool(body.get("include_working")))})
    if op == "preview":
        return _preview_artifact(h, body.get("id"))
    if op == "delete":
        aid = str(body.get("id") or "").strip()
        if not aid:
            return _bad(h, "缺少 id")
        return h._json({"ok": task_engine.delete_artifact(aid)})
    if op == "reveal":
        return _reveal_artifact(h, body.get("id"))
    return _bad(h, "未知操作：%s" % op)


def _resolve_artifact(art):
    """把产物解析成绝对路径并校验范围。返回 (real, err, code)。"""
    task = task_engine.get_task(art.get("task_id")) if art.get("task_id") else None
    sid = (task or {}).get("session_id") or ""
    s = find_session(load_sessions(), sid) if sid else None
    sandbox = agent.clean_sandbox((s or {}).get("sandbox")) if s else ""

    path = str(art.get("path") or "")
    if not os.path.isabs(path):
        if not sandbox:
            return "", "该产物没有可用的工作区，无法预览", 400
        path = os.path.join(sandbox, path)

    real = os.path.realpath(path)
    roots = [os.path.realpath(DATA_DIR)]
    if sandbox:
        roots.append(os.path.realpath(sandbox))
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        return "", "路径不在允许范围内", 403
    if not os.path.isfile(real):
        return "", "文件不存在", 404
    return real, "", 200


def _reveal_artifact(h, aid):
    """在资源管理器里打开产物所在文件夹（只定位，不执行文件）。"""
    art = task_engine.get_artifact(aid)
    if not art:
        return _bad(h, "找不到产物", 404)
    real, err, code = _resolve_artifact(art)
    if err:
        return _bad(h, err, code)
    opener = getattr(os, "startfile", None)
    if opener is None:
        return _bad(h, "当前系统不支持打开文件夹", 500)
    try:
        opener(os.path.dirname(real))
    except OSError as e:
        return _bad(h, "打开文件夹失败：%s" % e, 500)
    return h._json({"ok": True})


def _preview_artifact(h, aid):
    art = task_engine.get_artifact(aid)
    if not art:
        return _bad(h, "找不到产物", 404)
    real, err, code = _resolve_artifact(art)
    if err:
        return _bad(h, err, code)

    ext = os.path.splitext(real)[1].lower()
    size = os.path.getsize(real)
    if ext in _IMG_EXT:
        if size > _IMAGE_MAX:
            return _bad(h, "图片超过 5MB，暂不预览")
        with open(real, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        mime = mimetypes.guess_type(real)[0] or "image/png"
        return h._json({"ok": True, "artifact": art,
                        "preview": {"kind": "image",
                                    "data_url": "data:%s;base64,%s" % (mime, data)}})

    if ext in _TEXT_EXT or size <= _PREVIEW_MAX:
        with open(real, "rb") as f:
            raw = f.read(_PREVIEW_MAX)
        return h._json({"ok": True, "artifact": art,
                        "preview": {"kind": "text",
                                    "text": raw.decode("utf-8", errors="replace"),
                                    "truncated": size > _PREVIEW_MAX,
                                    "size": size}})
    return h._json({"ok": True, "artifact": art,
                    "preview": {"kind": "unsupported", "size": size}})
