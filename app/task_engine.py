# -*- coding: utf-8 -*-
"""任务引擎：把「一件事」拆成可执行、可暂停、可恢复的步骤。

状态机：
    task   pending → running → done / failed / canceled
                   ↘ paused ↗（暂停后恢复）
    step   pending → running → done / failed / blocked
                   ↘ skipped

安全约定（与产品决策一致）：
- **写操作必须人工确认**：能力权限为 write 的步骤第一次会停在 blocked，
  用户在界面上点「确认执行」后才继续；
- 不提供命令行能力；所有工具都来自 app.capabilities 的注册表；
- 进程重启后，原本 running 的任务统一转 paused，**不自动重跑**，
  避免同一写操作被执行两次。
"""
import json
import os
import re
import threading
import time
import uuid

from app import capabilities
from app import events as events_bus
from app import file_caps  # noqa: F401  注册文件能力（chat=False，仅任务引擎可调用）
from app import content_caps  # noqa: F401  注册笔记 / 搜索 / 文档能力
from app import stock_caps  # noqa: F401  注册模拟股市只读能力（对话助手可见）
from app import observe_caps  # noqa: F401  注册通用观测能力（observe.record / query）
from app import skill_caps    # noqa: F401  注册 Skill 读取能力（skill.read）
from app import bilibili  # noqa: F401  注册 B 站读取能力（bili.info / transcript / comments）
from app import llm_caps   # noqa: F401  注册 llm.generate（任务专用）
from app.capabilities import capability
from app.db import get_db

_MAX_STEPS = 20
_SEM = threading.BoundedSemaphore(2)     # 同时最多跑两个任务
_LOCK = threading.RLock()
_CONTROL = {}                            # tid -> {"pause": Event, "cancel": Event}
_THREADS = {}

WRITE_TOOLS = ("write_file", "edit_file", "make_dir", "move_file", "delete_file")


# ------------------------------------------------------------
# 内部工具
# ------------------------------------------------------------
def _now():
    return time.time()


def _render_args(obj, task):
    """把步骤参数里的 {{last}} / {{step.N.result}} 换成前面步骤的实际结果。

    N 从 1 开始（和界面上看到的序号一致）。这样「LLM 起草 → write_file」
    这种链就能串起来，不需要额外的变量存储。
    """
    steps = (task or {}).get("steps") or []
    done = [s for s in steps if s.get("status") == "done" and s.get("result")]
    ctx = {
        "last": (done[-1].get("result") if done else ""),
        "task.title": (task or {}).get("title") or "",
        "date": time.strftime("%Y-%m-%d"),
        "time": time.strftime("%H:%M"),
    }
    for i, s in enumerate(steps, 1):
        ctx["step.%d.result" % i] = s.get("result") or ""
        ctx["step.%d.title" % i] = s.get("title") or ""

    def repl(m):
        return str(ctx.get(m.group(1).strip(), ""))

    def walk(v):
        if isinstance(v, str):
            return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", repl, v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(obj)


def _loads(raw, default):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


def _task_dict(row):
    if row is None:
        return None
    d = dict(row)
    d["meta"] = _loads(d.get("meta"), {})
    return d


def _step_dict(row):
    d = dict(row)
    d["args"] = _loads(d.get("args"), {})
    d["confirmed"] = bool(d.get("confirmed"))
    return d


def _publish(tid):
    task = get_task(tid)
    if task:
        events_bus.publish({"type": "task_update", "task": task})


def _set_status(tid, status, error=None):
    fields, vals = ["status=?", "updated_at=?"], [status, _now()]
    if error is not None:
        fields.append("error=?")
        vals.append(str(error)[:2000])
    if status == "running":
        fields.append("started_at=CASE WHEN started_at=0 THEN ? ELSE started_at END")
        vals.append(_now())
    if status in ("done", "failed", "canceled"):
        fields.append("finished_at=?")
        vals.append(_now())
    vals.append(tid)
    conn = get_db()
    conn.execute("UPDATE tasks SET %s WHERE id=?" % ", ".join(fields), tuple(vals))
    conn.commit()
    if status in ("done", "canceled"):
        try:
            from app import calendar
            calendar.close_task_event(tid)
        except Exception as e:
            print("[任务] 关闭截止日程失败：%s" % e)


def _set_step(step_id, status, **fields):
    cols, vals = ["status=?"], [status]
    for k, v in fields.items():
        if k in ("result", "error", "retry", "started_at", "finished_at", "confirmed"):
            cols.append("%s=?" % k)
            vals.append(v)
    vals.append(step_id)
    conn = get_db()
    conn.execute("UPDATE task_steps SET %s WHERE id=?" % ", ".join(cols), tuple(vals))
    conn.commit()


_ARTIFACT_TOOLS = ("write_file", "edit_file", "move_file")
_WORKING_DIRS = {"tmp", "temp", ".tmp", "cache", ".cache", "__pycache__",
                 ".git", "node_modules", "build", "dist"}


def _artifact_kind(path):
    """区分交付物与工作区临时文件：临时目录、隐藏文件不算交付物。"""
    raw = str(path or "").replace("\\", "/")
    segs = [x for x in raw.lower().split("/") if x]
    if not segs:
        return "working"
    if segs[-1].startswith("."):
        return "working"
    # 绝对路径只看文件名：用户目录里也可能有 temp/ 之类的段，不能一刀切成临时文件
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        return "deliverable"
    if any(s in _WORKING_DIRS for s in segs[:-1]):
        return "working"
    return "deliverable"


def _artifact_abs(path, task):
    """把产物路径解析成绝对路径（相对路径按任务会话的沙箱解析）。"""
    path = str(path or "")
    if os.path.isabs(path):
        return path
    sandbox = ""
    sid = str((task or {}).get("session_id") or "")
    if sid:
        try:
            from app import agent
            from app.sessions import find_session, load_sessions
            s = find_session(load_sessions(), sid)
            sandbox = agent.clean_sandbox((s or {}).get("sandbox"))
        except Exception:
            sandbox = ""
    return os.path.join(sandbox, path) if sandbox else ""


def _add_artifact(task_id, step):
    tool = str(step.get("tool") or "")
    # 只有「产出文件」的工具才算产物：删除文件、建目录不该出现在产物栏
    if tool not in _ARTIFACT_TOOLS:
        return
    args = step.get("args") or {}
    path = str(args.get("path") or args.get("to") or "").strip()
    if not path:
        return
    task = get_task(task_id)
    real = _artifact_abs(path, task)
    if real and not os.path.isfile(real):
        return                              # 目录 / 已被删除，不登记
    size = 0
    if real:
        try:
            size = os.path.getsize(real)
        except OSError:
            size = 0
    kind = _artifact_kind(path)
    name = os.path.basename(path.rstrip("/\\")) or path
    conn = get_db()
    row = conn.execute("SELECT id FROM artifacts WHERE task_id=? AND path=?",
                       (str(task_id), path)).fetchone()
    if row:
        # 同一任务重复写同一路径：更新，不再插一条
        conn.execute(
            "UPDATE artifacts SET name=?,kind=?,size=?,created_at=? WHERE id=?",
            (name, kind, size, _now(), row["id"]))
    else:
        conn.execute(
            "INSERT INTO artifacts (id,task_id,kind,name,path,size,created_at,meta) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("a_" + uuid.uuid4().hex[:12], task_id, kind, name, path, size,
             _now(), "{}"))
    conn.commit()
    events_bus.publish({"type": "artifact_update"})


def _control(tid):
    with _LOCK:
        ctl = _CONTROL.get(tid)
        if ctl is None:
            ctl = {"pause": threading.Event(), "cancel": threading.Event()}
            _CONTROL[tid] = ctl
        return ctl


def _spawn(tid):
    # 旧线程刚把状态设成 paused、还没退出时，用户可能已经点了「确认执行」。
    # 这时如果直接 return，就再也没人跑这个任务了（表现为永远 running）。
    # 所以先等旧线程收尾，再决定是否起新线程。
    with _LOCK:
        th = _THREADS.get(tid)
    if th is not None and th.is_alive() and th is not threading.current_thread():
        th.join(timeout=2.0)
    with _LOCK:
        th = _THREADS.get(tid)
        if th is not None and th.is_alive():
            return
        th = threading.Thread(target=_run, args=(tid,), daemon=True,
                              name="task-%s" % tid)
        _THREADS[tid] = th
        th.start()


# ------------------------------------------------------------
# 公开接口
# ------------------------------------------------------------
def create_task(title, steps, session_id="", scene="general", intent="",
                due_at=None, remind_before=None, priority=0, tags=""):
    title = str(title or "").strip()
    if not title:
        raise ValueError("任务标题不能为空")

    if isinstance(tags, (list, tuple)):
        tags = ",".join(str(x).strip() for x in tags if str(x).strip())
    tags = str(tags or "").strip()
    try:
        priority = max(0, min(3, int(priority or 0)))
    except (TypeError, ValueError):
        priority = 0

    due_raw = str(due_at or "").strip()
    due_ts = None
    if due_raw:
        from app import calendar
        due_ts = calendar.parse_time(due_raw)
        if due_ts is None:
            raise ValueError("截止时间无法识别")

    remind = 600 if remind_before is None else max(0, int(remind_before))
    meta = {}
    if due_ts:
        meta["due_at"] = due_ts
        meta["remind_before"] = remind

    raw_steps = list(steps or [])
    if not raw_steps:
        raw_steps = [{"title": "执行任务"}]
    raw_steps = raw_steps[:_MAX_STEPS]

    tid = "k_" + uuid.uuid4().hex[:12]
    now = _now()
    conn = get_db()
    conn.execute(
        "INSERT INTO tasks (id,session_id,scene,title,intent,status,priority,tags,"
        "created_at,updated_at,started_at,finished_at,error,meta) "
        "VALUES (?,?,?,?,?, 'pending', ?,?, ?,?,0,0,'',?)",
        (tid, str(session_id or ""), str(scene or "general"), title,
         str(intent or ""), priority, tags, now, now,
         json.dumps(meta, ensure_ascii=False)))
    for i, st in enumerate(raw_steps):
        if isinstance(st, str):
            st = {"title": st}
        st = st if isinstance(st, dict) else {}
        conn.execute(
            "INSERT INTO task_steps (id,task_id,idx,title,tool,args,status,"
            "confirmed,result,error,retry,started_at,finished_at) "
            "VALUES (?,?,?,?,?,?, 'pending', ?, '', '', 0, 0, 0)",
            ("p_" + uuid.uuid4().hex[:12], tid, i,
             str(st.get("title") or ("步骤 %d" % (i + 1))),
             str(st.get("tool") or ""),
             json.dumps(st.get("args") or {}, ensure_ascii=False),
             1 if st.get("confirmed") else 0))
    conn.commit()
    if due_ts:
        try:
            from app import calendar
            ev = calendar.bind_task_event(
                tid, title, due_ts, remind_before=remind,
                session_id=str(session_id or ""))
            if ev:
                meta["event_id"] = ev["id"]
                conn.execute("UPDATE tasks SET meta=? WHERE id=?",
                             (json.dumps(meta, ensure_ascii=False), tid))
                conn.commit()
        except Exception as e:
            print("[任务] 同步截止日程失败：%s" % e)
    task = get_task(tid)
    _publish(tid)
    return task


def get_task(tid):
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (str(tid or ""),)).fetchone()
    if row is None:
        return None
    task = _task_dict(row)
    rows = conn.execute(
        "SELECT * FROM task_steps WHERE task_id=? ORDER BY idx", (task["id"],)
    ).fetchall()
    task["steps"] = [_step_dict(r) for r in rows]
    total = len(task["steps"])
    done = sum(1 for s in task["steps"] if s["status"] == "done")
    task["progress"] = {"total": total, "done": done,
                        "percent": int(done * 100 / total) if total else 0}
    return task


def list_tasks(status="", limit=50):
    sql = "SELECT id FROM tasks"
    args = []
    if status:
        sql += " WHERE status=?"
        args.append(str(status))
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(int(limit))
    ids = [r["id"] for r in get_db().execute(sql, tuple(args)).fetchall()]
    return [t for t in (get_task(i) for i in ids) if t]


def list_artifacts(task_id="", limit=50, include_working=False):
    """产物列表；默认只返回交付物，临时目录里的文件归为 working。"""
    conn = get_db()
    sql = ("SELECT a.*, t.title AS task_title FROM artifacts a "
           "LEFT JOIN tasks t ON t.id=a.task_id")
    where, vals = [], []
    if task_id:
        where.append("a.task_id=?")
        vals.append(str(task_id))
    if not include_working:
        where.append("a.kind<>'working'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    vals.append(int(limit))
    return [dict(r) for r in conn.execute(sql, tuple(vals)).fetchall()]


def get_artifact(aid):
    row = get_db().execute("SELECT * FROM artifacts WHERE id=?",
                           (str(aid or ""),)).fetchone()
    return dict(row) if row else None


def delete_artifact(aid):
    """只删登记记录，不动磁盘上的文件（避免误删用户成果）。"""
    conn = get_db()
    cur = conn.execute("DELETE FROM artifacts WHERE id=?", (str(aid or ""),))
    conn.commit()
    if cur.rowcount > 0:
        events_bus.publish({"type": "artifact_update"})
    return cur.rowcount > 0


def artifact_path(aid):
    """产物的绝对路径；无法解析时返回空串。"""
    art = get_artifact(aid)
    if not art:
        return ""
    path = str(art.get("path") or "")
    if os.path.isabs(path):
        return path
    task = get_task(art.get("task_id")) if art.get("task_id") else None
    return _artifact_abs(path, task)


def _reset_failed_steps(tid):
    """把失败步骤退回 pending（重试）；已完成的步骤保持不动。"""
    conn = get_db()
    cur = conn.execute(
        "UPDATE task_steps SET status='pending', result='', error='', "
        "retry=retry+1, started_at=0, finished_at=0 "
        "WHERE task_id=? AND status='failed'", (str(tid),))
    conn.execute("UPDATE tasks SET error='' WHERE id=?", (str(tid),))
    conn.commit()
    return cur.rowcount


def start_task(tid):
    task = get_task(tid)
    if task is None:
        return None
    if task["status"] in ("done", "canceled", "running"):
        return task
    # 失败任务再点「开始」= 重试失败步骤，否则会立刻又判失败
    if task["status"] == "failed":
        _reset_failed_steps(tid)
    ctl = _control(tid)
    ctl["pause"].clear()
    ctl["cancel"].clear()
    _set_status(tid, "running")
    _spawn(tid)
    _publish(tid)
    return get_task(tid)


def retry_task(tid):
    """重试失败任务：失败步骤回到 pending 后继续执行。"""
    task = get_task(tid)
    if task is None or task["status"] == "running":
        return task
    return start_task(tid)


def pause_task(tid):
    task = get_task(tid)
    if task is None or task["status"] not in ("running", "pending"):
        return task
    _control(tid)["pause"].set()
    _set_status(tid, "paused")
    _publish(tid)
    return get_task(tid)


def resume_task(tid):
    task = get_task(tid)
    if task is None or task["status"] in ("done", "canceled"):
        return task
    if task["status"] == "failed":
        _reset_failed_steps(tid)            # 「继续」对失败任务也应重试
    ctl = _control(tid)
    ctl["pause"].clear()
    ctl["cancel"].clear()
    _set_status(tid, "running")
    _spawn(tid)
    _publish(tid)
    return get_task(tid)


def cancel_task(tid):
    task = get_task(tid)
    if task is None:
        return None
    _control(tid)["cancel"].set()
    _set_status(tid, "canceled")
    _publish(tid)
    return get_task(tid)


def delete_task(tid):
    if get_task(tid) is None:
        return False
    _control(tid)["cancel"].set()
    try:
        from app import calendar
        calendar.delete_task_event(tid)
    except Exception as e:
        print("[任务] 删除截止日程失败：%s" % e)
    conn = get_db()
    conn.execute("DELETE FROM artifacts WHERE task_id=?", (str(tid),))
    conn.execute("DELETE FROM tasks WHERE id=?", (str(tid),))
    conn.commit()
    _CONTROL.pop(tid, None)
    _publish(tid)
    return True


def update_task(tid, title=None, priority=None, tags=None):
    """改任务标题 / 优先级 / 标签（运行中也可改，不影响执行）。"""
    task = get_task(tid)
    if task is None:
        return None
    sets, vals = [], []
    if title is not None:
        t = str(title).strip()
        if not t:
            raise ValueError("任务标题不能为空")
        sets.append("title=?")
        vals.append(t)
    if priority is not None:
        sets.append("priority=?")
        vals.append(max(0, min(3, int(priority))))
    if tags is not None:
        if isinstance(tags, (list, tuple)):
            tags = ",".join(str(x).strip() for x in tags if str(x).strip())
        sets.append("tags=?")
        vals.append(str(tags or ""))
    if not sets:
        return task
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(str(tid))
    conn = get_db()
    conn.execute("UPDATE tasks SET %s WHERE id=?" % ", ".join(sets), tuple(vals))
    conn.commit()
    _publish(tid)
    return get_task(tid)


_EDITABLE_TASK = ("pending", "paused", "failed")


def _editable(tid):
    """取一个允许编辑步骤的任务；不允许时抛 ValueError。"""
    task = get_task(tid)
    if task is None:
        raise ValueError("找不到任务")
    if task["status"] not in _EDITABLE_TASK:
        raise ValueError("任务正在运行或已结束，不能修改步骤")
    return task


def _renumber(conn, tid):
    rows = conn.execute("SELECT id FROM task_steps WHERE task_id=? ORDER BY idx",
                        (str(tid),)).fetchall()
    for i, r in enumerate(rows):
        conn.execute("UPDATE task_steps SET idx=? WHERE id=?", (i, r["id"]))


def _check_tool(tool):
    tool = str(tool or "").strip()
    if tool and capabilities.get(tool) is None:
        raise ValueError("未知能力：%s" % tool)
    return tool


def add_step(tid, title, tool="", args=None, position=None):
    task = _editable(tid)
    steps = task["steps"]
    if len(steps) >= _MAX_STEPS:
        raise ValueError("步骤最多 %d 个" % _MAX_STEPS)
    title = str(title or "").strip()
    if not title:
        raise ValueError("步骤标题不能为空")
    tool = _check_tool(tool)
    idx = len(steps) if position is None else max(0, min(len(steps), int(position)))
    conn = get_db()
    conn.execute("UPDATE task_steps SET idx=idx+1 WHERE task_id=? AND idx>=?",
                 (str(tid), idx))
    conn.execute(
        "INSERT INTO task_steps (id,task_id,idx,title,tool,args,status,confirmed,"
        "result,error,retry,started_at,finished_at) "
        "VALUES (?,?,?,?,?,?, 'pending',0,'','',0,0,0)",
        ("p_" + uuid.uuid4().hex[:12], str(tid), idx, title, tool,
         json.dumps(args or {}, ensure_ascii=False)))
    conn.commit()
    _publish(tid)
    return get_task(tid)


def update_step(tid, step_id, title=None, tool=None, args=None):
    task = _editable(tid)
    step = next((s for s in task["steps"] if s["id"] == step_id), None)
    if step is None:
        raise ValueError("找不到步骤")
    if step["status"] in ("done", "running"):
        raise ValueError("已完成 / 执行中的步骤不能修改")
    sets, vals = [], []
    if title is not None:
        t = str(title).strip()
        if not t:
            raise ValueError("步骤标题不能为空")
        sets.append("title=?")
        vals.append(t)
    if tool is not None:
        sets.append("tool=?")
        vals.append(_check_tool(tool))
        sets.append("confirmed=0")          # 换了工具要重新确认写操作
    if args is not None:
        sets.append("args=?")
        vals.append(json.dumps(args or {}, ensure_ascii=False))
        sets.append("confirmed=0")
    if step["status"] == "failed":
        sets.extend(["status='pending'", "error=''", "result=''",
                     "retry=retry+1", "started_at=0", "finished_at=0"])
    if not sets:
        return task
    vals.append(step_id)
    conn = get_db()
    conn.execute("UPDATE task_steps SET %s WHERE id=?" % ", ".join(sets), tuple(vals))
    conn.commit()
    _publish(tid)
    return get_task(tid)


def delete_step(tid, step_id):
    task = _editable(tid)
    step = next((s for s in task["steps"] if s["id"] == step_id), None)
    if step is None:
        raise ValueError("找不到步骤")
    if step["status"] in ("done", "running"):
        raise ValueError("已完成 / 执行中的步骤不能删除")
    if len(task["steps"]) <= 1:
        raise ValueError("至少保留一个步骤")
    conn = get_db()
    conn.execute("DELETE FROM task_steps WHERE id=? AND task_id=?",
                 (step_id, str(tid)))
    _renumber(conn, tid)
    conn.commit()
    _publish(tid)
    return get_task(tid)


def move_step(tid, step_id, delta):
    """上移 / 下移一个步骤（delta = -1 / +1）。"""
    task = _editable(tid)
    steps = task["steps"]
    i = next((n for n, s in enumerate(steps) if s["id"] == step_id), -1)
    if i < 0:
        raise ValueError("找不到步骤")
    j = i + (1 if int(delta or 1) > 0 else -1)
    if j < 0 or j >= len(steps):
        return task
    a, b = steps[i], steps[j]
    if a["status"] in ("done", "running") or b["status"] in ("done", "running"):
        raise ValueError("已完成 / 执行中的步骤不能移动")
    conn = get_db()
    conn.execute("UPDATE task_steps SET idx=? WHERE id=?", (b["idx"], a["id"]))
    conn.execute("UPDATE task_steps SET idx=? WHERE id=?", (a["idx"], b["id"]))
    conn.commit()
    _publish(tid)
    return get_task(tid)


def confirm_step(tid, step_id):
    """用户确认执行一个被拦下的写操作步骤"""
    task = get_task(tid)
    if task is None:
        return None
    step = next((s for s in task["steps"] if s["id"] == step_id), None)
    if step is None:
        return task
    _set_step(step_id, "pending", confirmed=1, error="")
    if task["status"] in ("paused", "failed"):
        return resume_task(tid)
    return get_task(tid)


def complete_step(tid, step_id):
    """手动完成一个步骤（没有绑定工具的步骤）"""
    task = get_task(tid)
    if task is None:
        return None
    step = next((s for s in task["steps"] if s["id"] == step_id), None)
    if step is None:
        return task
    _set_step(step_id, "done", finished_at=_now(), result="（用户标记完成）")
    _finish_or_pause(tid)
    task = get_task(tid)
    # 下一步如果是能用工具执行的，就自动接着跑；否则停在原地等用户继续勾。
    if task and task["status"] == "paused":
        nxt = next((s for s in task["steps"] if s["status"] == "pending"), None)
        if nxt and nxt.get("tool"):
            return resume_task(tid)
    return task


def recover():
    """进程重启后的收敛：running → paused，running 步骤 → pending。"""
    conn = get_db()
    conn.execute("UPDATE tasks SET status='paused' WHERE status='running'")
    conn.execute("UPDATE task_steps SET status='pending' WHERE status='running'")
    # 历史上删除任务时没有清产物，留下了一批指向不存在任务的记录
    conn.execute("DELETE FROM artifacts WHERE task_id<>'' AND task_id NOT IN "
                 "(SELECT id FROM tasks)")
    conn.commit()


# ------------------------------------------------------------
# 执行
# ------------------------------------------------------------
def _finish_or_pause(tid):
    task = get_task(tid)
    if task is None:
        return
    steps = task["steps"]
    status = ""
    if any(s["status"] in ("pending", "running", "blocked") for s in steps):
        status = "paused"
        _set_status(tid, status)
    elif any(s["status"] == "failed" for s in steps):
        status = "failed"
        _set_status(tid, status, error=task.get("error") or "有步骤执行失败")
    else:
        status = "done"
        _set_status(tid, status)

    if status == "done":
        # 完成任务写进长期记忆，之后可以直接用 memory.search 找回来
        try:
            from app import memory
            sid = task.get("session_id") or ""
            summary = "任务《%s》已完成：%s" % (
                task["title"], " → ".join(s["title"] for s in steps))
            memory.remember(summary, type="task", importance=0.45,
                            scope=("session:" + sid) if sid else "global",
                            source="task", source_id=tid)
        except Exception as e:
            print("[任务] 写入记忆失败：%s" % e)

    if status in ("done", "failed"):
        try:
            from app import workflow
            workflow.emit("task.done", {"task_id": tid, "title": task["title"],
                                        "status": status})
        except Exception as e:
            print("[任务] 触发工作流失败：%s" % e)

    _publish(tid)


def _run(tid):
    with _SEM:
        try:
            while True:
                task = get_task(tid)
                if task is None or task["status"] in ("canceled", "done", "failed"):
                    return
                ctl = _control(tid)
                if ctl["cancel"].is_set():
                    _set_status(tid, "canceled")
                    _publish(tid)
                    return
                if ctl["pause"].is_set():
                    _set_status(tid, "paused")
                    _publish(tid)
                    return

                step = next((s for s in task["steps"] if s["status"] == "pending"), None)
                if step is None:
                    _finish_or_pause(tid)
                    return

                # 没有绑定工具 = 需要用户自己完成的步骤
                if not step.get("tool"):
                    _set_status(tid, "paused")
                    _publish(tid)
                    return

                cap = capabilities.get(step["tool"])
                if cap is None:
                    _set_step(step["id"], "failed",
                              error="未知工具：%s" % step["tool"], finished_at=_now())
                    _set_status(tid, "failed", error="未知工具：%s" % step["tool"])
                    _publish(tid)
                    return

                # 写操作必须由用户点过「确认执行」
                if cap.permission == "write" and not step.get("confirmed"):
                    _set_step(step["id"], "blocked")
                    _set_status(tid, "paused")
                    _publish(tid)
                    return

                _set_step(step["id"], "running", started_at=_now())
                _publish(tid)
                text, _is_write = capabilities.invoke(
                    step["tool"], _render_args(step.get("args") or {}, task),
                    {"session_id": task.get("session_id") or "",
                     "task_id": tid, "source": "task"})
                ok = not text.lstrip().startswith("❌")
                _set_step(step["id"], "done" if ok else "failed",
                          result=text[:4000], error="" if ok else text[:1000],
                          finished_at=_now())
                if ok and step["tool"] in WRITE_TOOLS:
                    _add_artifact(tid, step)
                _publish(tid)
                if not ok:
                    _set_status(tid, "failed", error=text[:1000])
                    _publish(tid)
                    return
        except Exception as e:
            _set_status(tid, "failed", error="任务执行异常：%s" % e)
            _publish(tid)
        finally:
            with _LOCK:
                if _THREADS.get(tid) is threading.current_thread():
                    _THREADS.pop(tid, None)


# ------------------------------------------------------------
# 能力
# ------------------------------------------------------------
def _cap_create(args, ctx):
    steps = args.get("steps") or []
    if isinstance(steps, str):
        steps = [x.strip() for x in steps.splitlines() if x.strip()]
    task = create_task(args.get("title"), steps,
                       session_id=(ctx or {}).get("session_id") or "",
                       scene=args.get("scene") or "general",
                       intent=args.get("intent") or "",
                       due_at=args.get("due_at"),
                       remind_before=args.get("remind_before"),
                       priority=args.get("priority") or 0,
                       tags=args.get("tags") or "")
    started = False
    if args.get("auto_start", True):
        start_task(task["id"])
        started = True
    lines = ["已创建任务 %s：%s（%d 步）" % (task["id"], task["title"],
                                            len(task["steps"]))]
    for i, s in enumerate(task["steps"], 1):
        lines.append("%d. %s%s" % (i, s["title"],
                                   ("  [%s]" % s["tool"]) if s["tool"] else ""))
    meta = task.get("meta") or {}
    if meta.get("due_at"):
        lines.append("已同步截止日程：%s（提前 %d 分钟提醒）。" % (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(float(meta["due_at"]))),
            int(meta.get("remind_before") or 0) // 60))
    lines.append("已在工作台%s。" % ("启动" if started else "保存"))
    return "\n".join(lines)


def _cap_list(args, ctx):
    items = list_tasks(status=args.get("status") or "", limit=30)
    if not items:
        return "当前没有任务。"
    lines = ["共 %d 个任务：" % len(items)]
    for t in items:
        lines.append("- %s  %s  [%s]  %d/%d" % (
            t["id"], t["title"], t["status"],
            t["progress"]["done"], t["progress"]["total"]))
    return "\n".join(lines)


def _cap_get(args, ctx):
    t = get_task(args.get("id"))
    if not t:
        return "❌ 找不到任务：%s" % args.get("id")
    lines = ["%s（%s）" % (t["title"], t["status"])]
    for i, s in enumerate(t["steps"], 1):
        lines.append("%d. [%s] %s%s" % (i, s["status"], s["title"],
                                        ("  → " + s["result"][:120]) if s["result"] else ""))
    return "\n".join(lines)


capability("task.create",
           "创建一个可执行任务；有截止时间时传 due_at，系统会同步一条截止日程并提前提醒",
           {"title": "str", "steps": "list", "auto_start": "bool?",
            "intent": "str?", "due_at": "str?", "remind_before": "int?",
            "priority": "int?", "tags": "str?"},
           permission="write")(_cap_create)
capability("task.list", "列出任务及其进度", {"status": "str?"},
           permission="read")(_cap_list)
capability("task.get", "查看某个任务的步骤状态", {"id": "str"},
           permission="read")(_cap_get)
