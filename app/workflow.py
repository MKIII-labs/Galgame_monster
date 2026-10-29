# -*- coding: utf-8 -*-
"""工作台工作流：触发器（手动 / 定时 / 事件）→ 按顺序执行一串动作。

注意：这是工作台里的轻量自动化（建任务 / 通知 / 日程 / 发消息 / 朗读 / 调能力 / AI 只读巡检），
和对话里的「多阶段编排」（app/orchestrator.py，run_workflow 工具）不是同一套东西：
- 本模块面向 UI，动作固定、按顺序执行、结果写 workflow_runs；
- orchestrator 面向模型，节点组成 DAG、可断点续跑。

模板列表在重构中丢失，先保持为空；需要时按 TEMPLATES 的结构补回
（{id, name, desc, scene, trigger_type, trigger_at, trigger_weekdays, trigger_event, actions}）。
"""
import json
import threading
import time
import uuid

from app import agent_job, db, task_engine
from app import events as events_bus

ACTION_TYPES = ("task.create", "notify", "calendar.create", "session.message",
                "tts.speak", "capability", "agent.run")
TRIGGER_TYPES = ("manual", "time", "event")
EVENT_NAMES = ("task.done", "calendar.due")
TEMPLATES = []

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_SOURCE_REGISTERED = False


# ---------------------------------------------------------------- 读库

def _wf_from_row(row):
    wf = {k: row[k] for k in row.keys()}
    try:
        wf["actions"] = json.loads(wf.get("actions") or "[]")
    except (TypeError, ValueError):
        wf["actions"] = []
    wf["enabled"] = bool(wf.get("enabled"))
    return wf


def list_workflows():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM workflows ORDER BY created_at DESC").fetchall()
    return [_wf_from_row(r) for r in rows]


def get_workflow(wid):
    wid = str(wid or "").strip()
    if not wid:
        return None
    row = db.get_db().execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
    return _wf_from_row(row) if row else None


def list_runs(wid="", limit=20):
    try:
        limit = max(1, min(int(limit or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    conn = db.get_db()
    wid = str(wid or "").strip()
    if wid:
        rows = conn.execute(
            "SELECT * FROM workflow_runs WHERE workflow_id=? "
            "ORDER BY started_at DESC LIMIT ?", (wid, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workflow_runs ORDER BY started_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


# ---------------------------------------------------------------- 校验 / 写入

def _clean_actions(actions):
    out = []
    for a in (actions if isinstance(actions, list) else []):
        if not isinstance(a, dict):
            continue
        typ = str(a.get("type") or "").strip()
        if not typ:
            continue
        if typ not in ACTION_TYPES:
            raise ValueError("不支持的动作：%s" % typ)
        args = a.get("args") if isinstance(a.get("args"), dict) else {}
        out.append({"type": typ, "args": args})
    return out


def _parse_hhmm(text):
    try:
        h, m = str(text or "").strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (TypeError, ValueError):
        pass
    return None


def _next_time_run(wf, now=None):
    """下一次定时触发的时间戳；解析不了 / 永不触发返回 0。"""
    at = _parse_hhmm(wf.get("trigger_at"))
    if at is None:
        return 0
    now = time.time() if now is None else float(now)
    lt = time.localtime(now)
    days = [int(x) for x in str(wf.get("trigger_weekdays") or "").split(",")
            if str(x).strip().isdigit()]
    for offset in range(0, 8):
        cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + offset,
                            at[0], at[1], 0, 0, 0, -1))
        if cand <= now + 1:
            continue
        if days and time.localtime(cand).tm_wday not in days:
            continue
        return cand
    return 0


def _validate_trigger(trigger_type, trigger_at, trigger_event):
    if trigger_type not in TRIGGER_TYPES:
        raise ValueError("不支持的触发方式：%s" % trigger_type)
    if trigger_type == "time" and _parse_hhmm(trigger_at) is None:
        raise ValueError("定时触发需要 HH:MM 形式的时间")
    if trigger_type == "event" and trigger_event not in EVENT_NAMES:
        trigger_event = EVENT_NAMES[0]
    return trigger_event


def create_workflow(name, trigger_type="manual", trigger_at="",
                    trigger_weekdays="", trigger_event="", actions=None,
                    enabled=True):
    name = str(name or "").strip()
    if not name:
        raise ValueError("工作流名称不能为空")
    trigger_type = str(trigger_type or "manual").strip()
    trigger_at = str(trigger_at or "").strip()
    trigger_weekdays = str(trigger_weekdays or "").strip()
    trigger_event = str(trigger_event or "").strip()
    trigger_event = _validate_trigger(trigger_type, trigger_at, trigger_event)
    acts = _clean_actions(actions)
    if not acts:
        raise ValueError("至少添加一个动作")

    wid = "w_" + uuid.uuid4().hex[:12]
    now = time.time()
    wf = {"id": wid, "name": name, "trigger_type": trigger_type,
          "trigger_at": trigger_at, "trigger_weekdays": trigger_weekdays,
          "trigger_event": trigger_event, "actions": acts,
          "enabled": bool(enabled), "created_at": now, "updated_at": now,
          "last_run": 0, "next_run": 0}
    if trigger_type == "time" and enabled:
        wf["next_run"] = _next_time_run(wf, now)
    conn = db.get_db()
    conn.execute(
        "INSERT INTO workflows (id,name,trigger_type,trigger_at,trigger_weekdays,"
        "trigger_event,actions,enabled,last_run,next_run,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,0,?,?,?)",
        (wid, name, trigger_type, trigger_at, trigger_weekdays, trigger_event,
         json.dumps(acts, ensure_ascii=False), 1 if enabled else 0,
         wf["next_run"], now, now))
    conn.commit()
    return get_workflow(wid)


def update_workflow(wid, **fields):
    wf = get_workflow(wid)
    if not wf:
        return None
    name = str(fields.get("name", wf["name"]) or "").strip()
    if not name:
        raise ValueError("工作流名称不能为空")
    trigger_type = str(fields.get("trigger_type", wf["trigger_type"]) or "manual").strip()
    trigger_at = str(fields.get("trigger_at", wf["trigger_at"]) or "").strip()
    trigger_weekdays = str(fields.get("trigger_weekdays",
                                      wf["trigger_weekdays"]) or "").strip()
    trigger_event = str(fields.get("trigger_event", wf["trigger_event"]) or "").strip()
    trigger_event = _validate_trigger(trigger_type, trigger_at, trigger_event)
    actions = _clean_actions(fields.get("actions", wf["actions"]))
    if not actions:
        raise ValueError("至少添加一个动作")
    enabled = bool(fields.get("enabled", wf["enabled"]))
    now = time.time()
    next_run = 0
    if trigger_type == "time" and enabled:
        next_run = _next_time_run({"trigger_at": trigger_at,
                                   "trigger_weekdays": trigger_weekdays}, now)
    conn = db.get_db()
    conn.execute(
        "UPDATE workflows SET name=?,trigger_type=?,trigger_at=?,trigger_weekdays=?,"
        "trigger_event=?,actions=?,enabled=?,next_run=?,updated_at=? WHERE id=?",
        (name, trigger_type, trigger_at, trigger_weekdays, trigger_event,
         json.dumps(actions, ensure_ascii=False), 1 if enabled else 0,
         next_run, now, str(wid)))
    conn.commit()
    return get_workflow(wid)


def delete_workflow(wid):
    wid = str(wid or "").strip()
    if not wid:
        return False
    conn = db.get_db()
    cur = conn.execute("DELETE FROM workflows WHERE id=?", (wid,))
    conn.execute("DELETE FROM workflow_runs WHERE workflow_id=?", (wid,))
    conn.commit()
    return bool(cur.rowcount)


def set_enabled(wid, enabled):
    wf = get_workflow(wid)
    if not wf:
        return None
    return update_workflow(wid, enabled=bool(enabled))


# ---------------------------------------------------------------- 执行

def _placeholders(extra=None):
    lt = time.localtime()
    ctx = {"date": time.strftime("%Y-%m-%d", lt),
           "time": time.strftime("%H:%M", lt),
           "weekday": _WEEKDAYS[lt.tm_wday]}
    if extra:
        ctx.update(extra)
    return ctx


def _fill(text, ctx):
    s = str(text or "")
    for k, v in (ctx or {}).items():
        s = s.replace("{{%s}}" % k, str(v))
    return s


def _clip(text, n=800):
    t = str(text or "")
    return t if len(t) <= n else t[:max(0, n - 1)] + "…"


def _notify(title, text):
    events_bus.publish({"type": "notify", "kind": "automation",
                        "title": str(title or "工作流"),
                        "text": _clip(text, 600)})


def _execute_action(action, ctx):
    """执行一个动作，返回一行写进运行历史的文本。"""
    typ = str(action.get("type") or "")
    args = action.get("args") if isinstance(action.get("args"), dict) else {}

    if typ == "notify":
        title = _fill(args.get("title") or "工作流", ctx)
        text = _fill(args.get("text") or "", ctx)
        _notify(title, text)
        return "notify：%s" % _clip(title, 60)

    if typ == "task.create":
        from app import task_engine as te
        title = _fill(args.get("title") or "", ctx).strip()
        if not title:
            raise ValueError("创建任务缺少标题")
        steps = [_fill(s, ctx) for s in (args.get("steps") or []) if str(s).strip()]
        task = te.create_task(title, steps or [title],
                              session_id=str(args.get("session_id") or ""))
        if args.get("auto_start") and task:
            te.start_task(task["id"])
        return "task.create：%s" % _clip(title, 60)

    if typ == "calendar.create":
        from app import calendar
        title = _fill(args.get("title") or "", ctx).strip()
        if not title:
            raise ValueError("创建日程缺少标题")
        calendar.create_event(title, _fill(args.get("start_at") or "", ctx),
                              _fill(args.get("end_at") or "", ctx) or None)
        return "calendar.create：%s" % _clip(title, 60)

    if typ == "session.message":
        from app import scheduler as sched
        sid = str(args.get("session_id") or ctx.get("session_id") or "").strip()
        text = _fill(args.get("text") or "", ctx)
        if not sid:
            raise ValueError("发消息到会话缺少 session_id")
        if not sched._append_message(sid, text):
            raise ValueError("会话不存在：%s" % sid)
        return "session.message：%s" % _clip(text, 60)

    if typ == "tts.speak":
        from app import tts
        text = _fill(args.get("text") or "", ctx)
        ok, msg = tts.speak_local(text)
        if not ok:
            raise ValueError("朗读失败：%s" % msg)
        return "tts.speak：%s" % _clip(text, 60)

    if typ == "capability":
        from app import capabilities
        cid = str(args.get("id") or "").strip()
        inner = args.get("args") or {}
        if isinstance(inner, str):
            try:
                inner = json.loads(inner) if inner.strip() else {}
            except ValueError:
                raise ValueError("能力参数不是合法 JSON")
        text, _is_write = capabilities.invoke(cid, _fill_value(inner, ctx), {})
        if str(text).startswith("❌"):
            raise ValueError(text)
        return "capability[%s]：%s" % (cid, _clip(text, 200))

    if typ == "agent.run":
        prompt = _fill(args.get("prompt") or "", ctx).strip()
        sandbox = _fill(args.get("sandbox") or "", ctx).strip()
        if not prompt:
            raise ValueError("AI 只读巡检缺少提示词")
        r = agent_job.run(prompt, sandbox,
                          max_rounds=int(args.get("max_rounds") or 6))
        text = str(r.get("text") or "")
        report = str(args.get("report") or "findings")
        if report == "always" or r.get("findings"):
            _notify("AI 巡检：%s" % (ctx.get("workflow") or ""), text)
            return "agent:" + _clip(text, 800)
        return "agent:skipped（无发现）"

    raise ValueError("不支持的动作：%s" % typ)


def _fill_value(value, ctx):
    if isinstance(value, str):
        return _fill(value, ctx)
    if isinstance(value, list):
        return [_fill_value(x, ctx) for x in value]
    if isinstance(value, dict):
        return {k: _fill_value(x, ctx) for k, x in value.items()}
    return value


def _execute(wf, run_id):
    wf = dict(wf or {})
    ctx = _placeholders({"workflow": wf.get("name") or "",
                         "session_id": ""})
    lines, status = [], "done"
    try:
        for action in wf.get("actions") or []:
            try:
                line = _execute_action(action, ctx)
            except Exception as e:
                lines.append("%s 失败：%s" % (action.get("type"), e))
                status = "failed"
                break
            if line:
                lines.append(line)
    finally:
        try:
            conn = db.get_db()
            conn.execute(
                "UPDATE workflow_runs SET finished_at=?,status=?,detail=? WHERE id=?",
                (time.time(), status, "\n".join(lines), run_id))
            conn.commit()
        except Exception as e:
            print("[工作流] 写运行记录失败：%s" % e)


def run_workflow(wid):
    """触发一次运行（后台线程），返回 run_id；找不到工作流返回空串。"""
    wf = get_workflow(wid)
    if not wf:
        return ""
    rid = "r_" + uuid.uuid4().hex[:12]
    now = time.time()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO workflow_runs (id,workflow_id,started_at,finished_at,status,detail) "
        "VALUES (?,?,?,0,'running','')", (rid, wf["id"], now))
    next_run = 0
    if wf["trigger_type"] == "time":
        next_run = _next_time_run(wf, now)
    conn.execute("UPDATE workflows SET last_run=?,next_run=? WHERE id=?",
                 (now, next_run, wf["id"]))
    conn.commit()
    threading.Thread(target=_execute, args=(wf, rid), daemon=True).start()
    return rid


def emit(event, payload=None):
    """事件触发：把事件派发给订阅它的工作流（供任务完成 / 日程到点调用）。"""
    event = str(event or "").strip()
    if event not in EVENT_NAMES:
        return 0
    payload = payload if isinstance(payload, dict) else {}
    rows = db.get_db().execute(
        "SELECT * FROM workflows WHERE enabled=1 AND trigger_type='event' "
        "AND trigger_event=?", (event,)).fetchall()
    for row in rows:
        _run_wf_with_payload(_wf_from_row(row), payload)
    return len(rows)


def _run_wf_with_payload(wf, payload):
    rid = "r_" + uuid.uuid4().hex[:12]
    now = time.time()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO workflow_runs (id,workflow_id,started_at,finished_at,status,detail) "
        "VALUES (?,?,?,0,'running','')", (rid, wf["id"], now))
    conn.execute("UPDATE workflows SET last_run=? WHERE id=?", (now, wf["id"]))
    conn.commit()

    def work():
        ctx = _placeholders({("event.%s" % k): v for k, v in (payload or {}).items()})
        ctx["workflow"] = wf.get("name") or ""
        lines, status = [], "done"
        for action in wf.get("actions") or []:
            try:
                lines.append(_execute_action(action, ctx))
            except Exception as e:
                lines.append("%s 失败：%s" % (action.get("type"), e))
                status = "failed"
                break
        conn2 = db.get_db()
        conn2.execute(
            "UPDATE workflow_runs SET finished_at=?,status=?,detail=? WHERE id=?",
            (time.time(), status, "\n".join(lines), rid))
        conn2.commit()

    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- 定时源

def _tick(now_ts=None):
    """由 scheduler 每轮调用：补 next_run / 到点触发。"""
    now = time.time() if now_ts is None else float(now_ts)
    try:
        conn = db.get_db()
        rows = conn.execute("SELECT * FROM workflows WHERE enabled=1").fetchall()
    except Exception:
        return
    for row in rows:
        wf = _wf_from_row(row)
        if wf.get("trigger_type") != "time":
            continue
        nxt = float(wf.get("next_run") or 0)
        if not nxt:
            nxt = _next_time_run(wf, now)
            conn.execute("UPDATE workflows SET next_run=? WHERE id=?", (nxt, wf["id"]))
            conn.commit()
            continue
        if nxt <= now:
            run_workflow(wf["id"])


def _register_tick():
    global _SOURCE_REGISTERED
    if _SOURCE_REGISTERED:
        return
    try:
        from app import scheduler
        scheduler.add_source(_tick)
        _SOURCE_REGISTERED = True
    except Exception as e:
        print("[工作流] 注册定时源失败：%s" % e)


_register_tick()
