# -*- coding: utf-8 -*-
"""工作台 API：任务 / 产物 / 场景（由 app.registry 自注册）"""
import base64
import json
import mimetypes
import os

from app import agent, calendar, capabilities, scenes, task_engine, work_extras
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
