# -*- coding: utf-8 -*-
"""定时任务调度：定时提醒。

任务落在 data/tasks.json（独立文件，不塞进会话存储）。一个守护线程每
SCAN_INTERVAL 秒扫一次到期任务。

分工（别搞重，否则会重复通知）：
- **系统通知**：reminder 由 Windows 计划任务负责（app/notify.py）——程序没开也能弹；
  这里的调度器**不再弹一次**。
- **会话消息 + 前端实时插入**：由这里负责（追加进 messages + publish 事件）。
"""
import datetime
import threading
import time
import uuid

from app import events, notify
from app.store import TASKS_FILE, read_json, write_json

SCAN_INTERVAL = 15       # 秒
STALE_AFTER = 86400      # 一次性提醒错过超过一天（关机等）就不再补弹

_lock = threading.RLock()
_started = False
_thread = None
_SOURCES = []            # 额外的到期检查（如日程），由模块自注册


def _new_id():
    return "t_" + uuid.uuid4().hex[:10]


_TIME_FMT = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M")


def parse_at(v):
    """把时间解析成 epoch 秒：支持 epoch 数字与几种常见字符串；失败返回 None。

    （模型的 set_reminder 与前端表单都走这里，保证两处口径一致）
    """
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in _TIME_FMT:
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _now():
    return time.time()


def switch_on(key, default=True):
    """提醒开关：缺失 / True 都算开。调度器与日历共用同一套判定。"""
    try:
        from app.config import load_config
        cfg = load_config() or {}
    except Exception:                    # noqa: BLE001  读配置失败按默认放行
        return default
    v = cfg.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off")
    return bool(v)


# ------------------------------------------------------------
# 存取
# ------------------------------------------------------------
def load_tasks():
    data = read_json(TASKS_FILE, None)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        data = {"items": []}
    return data


def save_tasks(data):
    write_json(TASKS_FILE, data, indent=None)


def list_tasks():
    with _lock:
        return list(load_tasks()["items"])


def find_task(task_id):
    for t in list_tasks():
        if t.get("id") == task_id:
            return t
    return None


def _update(task_id, **fields):
    """在锁内改一条任务的字段，返回改后的任务（找不到返回 None）"""
    with _lock:
        data = load_tasks()
        hit = next((t for t in data["items"] if t.get("id") == task_id), None)
        if hit is None:
            return None
        hit.update(fields)
        save_tasks(data)
        return hit


# ------------------------------------------------------------
# 增删改
# ------------------------------------------------------------
def _default_title(text):
    t = (text or "").strip()
    return ("提醒：" + t[:24]) if t else "提醒"


def add_task(session_id, at, text="", kind="reminder", repeat="", title=""):
    """新增提醒；reminder 会同时注册 Windows 计划任务（系统通知的唯一来源）。"""
    task = {
        "id": _new_id(),
        "session_id": session_id,
        "kind": kind,
        "at": float(at),
        "text": text or "",
        "title": title or _default_title(text),
        "repeat": repeat or "",
        "enabled": True,
        "paused": False,
        "silent": 0,
        "created_at": _now(),
        "last_fired": 0.0,
        "fired": 0,
    }
    with _lock:
        data = load_tasks()
        data["items"].append(task)
        save_tasks(data)
    if kind == "reminder":
        _sync_system_task(task)
    return task


def remove_task(task_id, unregister=True):
    with _lock:
        data = load_tasks()
        hit = next((t for t in data["items"] if t.get("id") == task_id), None)
        data["items"] = [t for t in data["items"] if t.get("id") != task_id]
        save_tasks(data)
    if hit:
        if unregister and hit.get("kind") == "reminder":
            notify.unregister(task_id)
            notify.cleanup_payload(task_id)
    return bool(hit)


def set_enabled(task_id, on):
    hit = _update(task_id, enabled=bool(on), paused=False)
    if not hit:
        return None
    if hit.get("kind") == "reminder":
        if on:
            _sync_system_task(hit)
        else:
            notify.unregister(task_id)
    return hit


# ------------------------------------------------------------
# 系统计划任务同步
# ------------------------------------------------------------
def _sync_system_task(task):
    """把 reminder 同步成一次性 Windows 计划任务；失败只提示，不影响会话内提醒。"""
    if task.get("kind") != "reminder" or not notify.available():
        return False
    if not switch_on("reminders_enabled") or not switch_on("reminder_system_enabled"):
        return False
    at = datetime.datetime.fromtimestamp(float(task.get("at") or 0))
    ok, info = notify.register(task["id"], at,
                               task.get("title") or "提醒", task.get("text") or "")
    if not ok:
        print("[提醒] 注册系统计划任务失败：%s" % info)
    return ok


def apply_switches():
    """设置里的提醒开关变化后，按当前开关同步已有提醒的 Windows 计划任务。

    总开关或「系统通知」关掉时，把已注册的计划任务一并注销——否则程序关了
    Windows 仍会弹，用户会以为开关没生效。
    """
    system_on = switch_on("reminders_enabled") and switch_on("reminder_system_enabled")
    for task in list_tasks():
        if task.get("kind") != "reminder":
            continue
        try:
            if system_on and task.get("enabled") and not task.get("paused"):
                _sync_system_task(task)
            else:
                notify.unregister(task["id"])
        except Exception as e:           # noqa: BLE001  单个任务失败不影响其它提醒
            print("[提醒] 同步开关失败 %s：%s" % (task.get("id"), e))


# ------------------------------------------------------------
# 触发
# ------------------------------------------------------------
def _append_message(sid, content, auto=True):
    """往会话追加一条消息（auto=True 标记为系统/本程序主动发出）。"""
    from app.sessions import load_sessions, save_sessions, find_session
    from app.store import _lock as store_lock
    data = load_sessions()
    s = find_session(data, sid)
    if not s:
        return False
    msg = {"role": "assistant", "content": content}
    if auto:
        msg["auto"] = True
    s.setdefault("messages", []).append(msg)
    s["updated_at"] = _now()
    with store_lock:
        save_sessions(data)
    return True


def _retire(task):
    """一次性任务：触发后从库里删掉"""
    notify.cleanup_payload(task["id"])
    with _lock:
        data = load_tasks()
        data["items"] = [t for t in data["items"] if t.get("id") != task["id"]]
        save_tasks(data)


def _fire_reminder(task, now, deliver=True):
    """deliver=False：应用内提醒关掉时，只推进重复计划，不写会话、不推前端。"""
    body = (task.get("text") or "").strip() or "时间到了"
    if deliver:
        _append_message(task["session_id"], "⏰ **提醒**：" + body)
        events.publish({"type": "notify", "kind": "reminder",
                        "session_id": task["session_id"], "task_id": task["id"],
                        "title": task.get("title") or "提醒", "text": body})
    if task.get("repeat") in ("daily", "weekly"):
        step = 86400 if task.get("repeat") == "daily" else 7 * 86400
        nxt = float(task.get("at") or now)
        while nxt <= now:
            nxt += step
        hit = _update(task["id"], at=nxt, last_fired=now,
                      fired=int(task.get("fired") or 0) + 1)
        if hit:
            _sync_system_task(hit)
    else:
        _retire(task)


def tick(now=None):
    """扫一遍到期任务，返回本轮触发的任务 id 列表（测试用）。"""
    now = now or _now()
    if not switch_on("reminders_enabled"):
        return []                        # 总开关关闭：提醒与外部来源全部暂停
    fired = []
    for task in list_tasks():
        if not task.get("enabled") or task.get("paused"):
            continue
        at = float(task.get("at") or 0)
        if at <= 0 or at > now:
            continue
        if task.get("kind") != "reminder":
            continue                     # 旧版主动聊天任务：功能已下线，忽略
        if (now - at) > STALE_AFTER:
            _retire(task)                     # 错过太久：直接作废，不补弹
            continue
        try:
            _fire_reminder(task, now, deliver=switch_on("reminder_inapp_enabled"))
            fired.append(task["id"])
        except Exception as e:
            print("[调度] 任务 %s 触发失败：%s" % (task.get("id"), e))
    # 外部来源（日程等）：它们自己负责去重与推送，这里只保证异常不拖垮调度线程
    for fn in list(_SOURCES):
        try:
            fn(now)
        except Exception as e:
            print("[调度] 外部来源 %s 失败：%s" % (getattr(fn, "__name__", fn), e))
    return fired


def add_source(fn):
    """注册一个「每轮 tick 都会被调用」的检查函数（日程提醒用）。"""
    if callable(fn) and fn not in _SOURCES:
        _SOURCES.append(fn)


def _loop():
    while True:
        try:
            tick()
        except Exception as e:
            print("[调度] 循环异常：%s" % e)
        time.sleep(SCAN_INTERVAL)


def start():
    """启动调度线程（create_server 里调用；重复调用只生效一次）"""
    global _started, _thread
    with _lock:
        if _started:
            return
        _started = True
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print("[调度] 已启动，当前任务数：%d" % len(list_tasks()))
