# -*- coding: utf-8 -*-
"""工作台扩展：任务模板、专注计时记录、周报统计。

与 task_engine 分开，避免主执行器继续变厚；这里只做「读多写少」的
统计与模板数据，不参与任务状态机。
"""
import json
import time
import uuid

from app.db import get_db


def _now():
    return time.time()


def _day_start(ts):
    """本地时区的当天 00:00（与前端 workDayStart 一致）。"""
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


# ---------------------------------------------------------------- 任务模板
def list_templates():
    rows = get_db().execute(
        "SELECT * FROM task_templates ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["steps"] = json.loads(d.get("steps") or "[]")
        except (ValueError, TypeError):
            d["steps"] = []
        out.append(d)
    return out


def save_template(name, title, steps, tags="", priority=0, estimate_min=0, tid=""):
    name = str(name or "").strip()[:60]
    title = str(title or "").strip()[:200]
    if not name or not title:
        raise ValueError("模板名称与任务标题不能为空")
    raw_steps = []
    for st in (steps or [])[:20]:
        if isinstance(st, str):
            raw_steps.append({"title": st.strip()})
        elif isinstance(st, dict):
            raw_steps.append({"title": str(st.get("title") or "").strip(),
                              "tool": str(st.get("tool") or ""),
                              "args": st.get("args") or {}})
    raw_steps = [s for s in raw_steps if s["title"]]
    if not raw_steps:
        raw_steps = [{"title": "执行任务"}]
    try:
        priority = max(0, min(3, int(priority or 0)))
    except (TypeError, ValueError):
        priority = 0
    if isinstance(tags, (list, tuple)):
        tags = ",".join(str(x).strip() for x in tags if str(x).strip())
    now = _now()
    conn = get_db()
    if tid:
        conn.execute(
            "UPDATE task_templates SET name=?,title=?,steps=?,tags=?,priority=?,"
            "estimate_min=?,updated_at=? WHERE id=?",
            (name, title, json.dumps(raw_steps, ensure_ascii=False), str(tags or ""),
             priority, max(0, int(estimate_min or 0)), now, str(tid)))
        out_id = str(tid)
    else:
        out_id = "t_" + uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO task_templates (id,name,title,steps,tags,priority,"
            "estimate_min,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (out_id, name, title, json.dumps(raw_steps, ensure_ascii=False),
             str(tags or ""), priority, max(0, int(estimate_min or 0)), now, now))
    conn.commit()
    return get_template(out_id)


def get_template(tid):
    row = get_db().execute("SELECT * FROM task_templates WHERE id=?",
                           (str(tid or ""),)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["steps"] = json.loads(d.get("steps") or "[]")
    except (ValueError, TypeError):
        d["steps"] = []
    return d


def delete_template(tid):
    cur = get_db().execute("DELETE FROM task_templates WHERE id=?",
                           (str(tid or ""),))
    get_db().commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------- 专注计时
def add_focus(task_id="", title="", start_at=0, end_at=0, minutes=0, kind="focus"):
    """记一次专注 / 休息。前端在计时结束时提交实际区间。"""
    end_at = float(end_at or _now())
    start_at = float(start_at or 0)
    if start_at <= 0:
        start_at = end_at - max(0, int(minutes or 0)) * 60
    if minutes is None or int(minutes or 0) <= 0:
        minutes = max(0, int(round((end_at - start_at) / 60.0)))
    minutes = min(24 * 60, int(minutes or 0))
    if minutes <= 0:
        raise ValueError("专注时长必须大于 0 分钟")
    fid = "f_" + uuid.uuid4().hex[:12]
    conn = get_db()
    conn.execute(
        "INSERT INTO focus_sessions (id,task_id,title,start_at,end_at,minutes,kind,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (fid, str(task_id or ""), str(title or "")[:200], start_at, end_at,
         minutes, "break" if kind == "break" else "focus", _now()))
    conn.commit()
    return {"id": fid, "task_id": str(task_id or ""), "title": str(title or ""),
            "start_at": start_at, "end_at": end_at, "minutes": minutes,
            "kind": "break" if kind == "break" else "focus"}


def list_focus(start_at=0, end_at=0, limit=200):
    sql = "SELECT * FROM focus_sessions"
    where, args = [], []
    if start_at:
        where.append("end_at>=?")
        args.append(float(start_at))
    if end_at:
        where.append("start_at<=?")
        args.append(float(end_at))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_at DESC LIMIT ?"
    args.append(max(1, min(1000, int(limit or 200))))
    return [dict(r) for r in get_db().execute(sql, tuple(args)).fetchall()]


# ---------------------------------------------------------------- 周报统计
def summary(days=7):
    """近 N 天的任务 / 专注 / 逾期概览，供工作台「周报」卡片使用。"""
    days = max(1, min(90, int(days or 7)))
    now = _now()
    start = _day_start(now) - (days - 1) * 86400
    end_days = [_day_start(start + i * 86400) for i in range(days)]
    conn = get_db()

    tasks = [dict(r) for r in conn.execute(
        "SELECT id,title,status,priority,tags,due_at,parent_id,depends_on,"
        "created_at,updated_at,finished_at FROM tasks").fetchall()]
    open_states = ("pending", "running", "paused", "failed")
    done_by_day = [0] * days
    created_by_day = [0] * days
    for t in tasks:
        fin = float(t.get("finished_at") or 0)
        if t.get("status") == "done" and fin >= start:
            idx = int((_day_start(fin) - start) / 86400)
            if 0 <= idx < days:
                done_by_day[idx] += 1
        cre = float(t.get("created_at") or 0)
        if cre >= start:
            idx = int((_day_start(cre) - start) / 86400)
            if 0 <= idx < days:
                created_by_day[idx] += 1

    focus = list_focus(start_at=start, end_at=now, limit=1000)
    focus_by_day = [0] * days
    focus_total = 0
    for f in focus:
        if f.get("kind") == "break":
            continue
        focus_total += int(f.get("minutes") or 0)
        fin = float(f.get("end_at") or 0)
        idx = int((_day_start(fin) - start) / 86400)
        if 0 <= idx < days:
            focus_by_day[idx] += int(f.get("minutes") or 0)

    overdue = [t for t in tasks
               if t.get("status") in open_states
               and float(t.get("due_at") or 0) > 0
               and float(t.get("due_at") or 0) < now]
    status_count = {}
    for t in tasks:
        status_count[t.get("status") or "pending"] = \
            status_count.get(t.get("status") or "pending", 0) + 1
    tag_count = {}
    for t in tasks:
        if t.get("status") not in open_states:
            continue
        for tag in str(t.get("tags") or "").replace("，", ",").split(","):
            tag = tag.strip()
            if tag:
                tag_count[tag] = tag_count.get(tag, 0) + 1

    return {
        "days": days,
        "start_at": start,
        "end_at": now,
        "days_at": end_days,
        "done_by_day": done_by_day,
        "created_by_day": created_by_day,
        "focus_by_day": focus_by_day,
        "focus_minutes": focus_total,
        "focus_sessions": len([f for f in focus if f.get("kind") != "break"]),
        "overdue": len(overdue),
        "overdue_tasks": [{"id": t["id"], "title": t["title"],
                           "due_at": t.get("due_at")} for t in overdue[:8]],
        "status_count": status_count,
        "tag_count": sorted(tag_count.items(), key=lambda kv: -kv[1])[:8],
    }
