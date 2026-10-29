# -*- coding: utf-8 -*-
"""定时任务调度：定时提醒 + AI 主动聊天。

任务落在 data/tasks.json（独立文件，不塞进 sessions.json）。一个守护线程每
SCAN_INTERVAL 秒扫一次到期任务。

分工（别搞重，否则会重复通知）：
- **系统通知**：reminder 由 Windows 计划任务负责（app/notify.py）——程序没开也能弹；
  这里的调度器**不再弹一次**。
- **会话消息 + 前端实时插入**：由这里负责（追加进 messages + publish 事件）。
- **主动聊天**：需要调模型，只能在本程序运行时发生（程序没开就没有）。
"""
import datetime
import random
import threading
import time
import uuid

from app import events, notify
from app.store import TASKS_FILE, read_json, write_json

SCAN_INTERVAL = 15       # 秒
STALE_AFTER = 86400      # 一次性提醒错过超过一天（关机等）就不再补弹
COOLDOWN = 180           # 用户刚有活动时，主动聊天先让路（秒）
MAX_SILENT = 2           # 连续这么多次主动都没得到回应就自动暂停

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
def _default_title(kind, text):
    if kind == "auto_chat":
        return "主动聊天"
    t = (text or "").strip()
    return ("提醒：" + t[:24]) if t else "提醒"


def add_task(session_id, at, text="", kind="reminder", repeat="",
             title="", gap_min=40, gap_max=120):
    """新增任务；reminder 会同时注册 Windows 计划任务（系统通知的唯一来源）。"""
    task = {
        "id": _new_id(),
        "session_id": session_id,
        "kind": kind,
        "at": float(at),
        "text": text or "",
        "title": title or _default_title(kind, text),
        "repeat": repeat or "",
        "enabled": True,
        "paused": False,
        "silent": 0,
        "created_at": _now(),
        "last_fired": 0.0,
        "fired": 0,
    }
    if kind == "auto_chat":
        task["gap_min"] = int(gap_min or 40)
        task["gap_max"] = int(gap_max or 120)
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
    at = datetime.datetime.fromtimestamp(float(task.get("at") or 0))
    ok, info = notify.register(task["id"], at,
                               task.get("title") or "提醒", task.get("text") or "")
    if not ok:
        print("[提醒] 注册系统计划任务失败：%s" % info)
    return ok


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


def _fire_reminder(task, now):
    body = (task.get("text") or "").strip() or "时间到了"
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


def _user_active_recently(sid, secs):
    """会话最近是否刚有活动（用作「别在用户刚说完话时插嘴」的粗略判断）"""
    from app.sessions import load_sessions, find_session
    s = find_session(load_sessions(), sid)
    if not s:
        return False
    return (_now() - float(s.get("updated_at") or 0)) < secs


def _last_is_auto(sid):
    from app.sessions import load_sessions, find_session
    s = find_session(load_sessions(), sid)
    msgs = (s or {}).get("messages") or []
    return bool(msgs and msgs[-1].get("auto"))


def _next_auto_at(task, now):
    lo = int(task.get("gap_min") or 40)
    hi = int(task.get("gap_max") or 120)
    if hi < lo:
        hi = lo
    return now + random.randint(lo, hi) * 60


def _reschedule_auto(task, now, silent=None, pause=False):
    fields = {"at": _next_auto_at(task, now), "last_fired": now,
              "fired": int(task.get("fired") or 0) + 1}
    if silent is not None:
        fields["silent"] = int(silent)
    if pause:
        fields["paused"] = True
    _update(task["id"], **fields)


def _fire_auto(task, now):
    sid = task["session_id"]
    # 首次（开启后的开场那条）不做冷却判断 —— 否则你刚开完开关、要的即时反馈会被顺延掉
    if task.get("fired") and _user_active_recently(sid, COOLDOWN):
        _reschedule_auto(task, now)          # 用户刚在说话，先让路
        return
    if _last_is_auto(sid):
        silent = int(task.get("silent") or 0) + 1
        if silent >= MAX_SILENT:
            print("[主动聊天] 连续 %d 次无回应，自动暂停（会话 %s）" % (silent, sid))
            _reschedule_auto(task, now, silent=silent, pause=True)
            return
        _update(task["id"], silent=silent)
    else:
        _update(task["id"], silent=0)
    text = generate_auto_message(sid)
    if not text:
        _reschedule_auto(task, now)
        return
    _append_message(sid, text)
    events.publish({"type": "auto_msg", "session_id": sid, "text": text,
                    "character_id": _char_id(sid)})
    _reschedule_auto(task, now, silent=0)


def _char_id(sid):
    from app.sessions import load_sessions, find_session
    s = find_session(load_sessions(), sid)
    return (s or {}).get("gal_character_id") or ""


def send_now(sid):
    """立刻生成并发送一条主动消息（供设置里的「立即试一次」用）。返回文本或空串。

    与定时触发的差别：不做冷却判断、不改任务的下次时间。
    """
    text = generate_auto_message(sid)
    if not text:
        return ""
    _append_message(sid, text)
    events.publish({"type": "auto_msg", "session_id": sid, "text": text,
                    "character_id": _char_id(sid)})
    return text


def generate_auto_message(sid):
    """让模型生成一句「主动开口」的话；失败返回空串（调用方按跳过处理）。"""
    from app.config import get_api_conf
    from app.llm import _quick_chat, recent_dialog
    from app.sessions import load_sessions, find_session
    s = find_session(load_sessions(), sid)
    if not s:
        return ""
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return ""
    msgs = [{"role": "system", "content": _auto_system_prompt(s)}]
    recent = recent_dialog(s, limit=8, each=120)
    if recent:
        msgs.append({"role": "user",
                     "content": "（以下是你们最近聊过的内容，仅供你避免重复，不要引用）\n" + recent})
    msgs.append({"role": "user", "content": "现在请你主动开口，发一句话。"})
    text = _quick_chat(api_key, api_url, api_model, msgs,
                       max_tokens=200, source="auto_chat")
    return (text or "").strip()


def _auto_system_prompt(s):
    name, persona = "助手", ""
    if s.get("gal_enabled") and s.get("gal_character_id"):
        try:
            from app import gal
            ch = gal.load_character(s.get("gal_character_id"))
            if ch:
                name = ch.get("name") or "角色"
                persona = (ch.get("profile") or "")[:1500]
        except Exception:
            pass
    return "\n".join([x for x in [
        "你是「%s」。现在由你**主动**给对方发消息。" % name,
        persona,
        "要求：",
        "1. 只输出你这一句话，不要解释、不要加引号、不要写旁白说明；",
        "2. 像真人发消息一样：口语、简短（20–60 字），可以是个小话题、关心、吐槽或分享；",
        "3. **不要复述**之前聊过的内容，不要连着追问，也不要问「在吗」「在干什么」这类空话；",
        "4. 不要提「我是 AI」「根据设定」之类的话。",
    ] if x])


def tick(now=None):
    """扫一遍到期任务，返回本轮触发的任务 id 列表（测试用）。"""
    now = now or _now()
    fired = []
    for task in list_tasks():
        if not task.get("enabled") or task.get("paused"):
            continue
        at = float(task.get("at") or 0)
        if at <= 0 or at > now:
            continue
        if task.get("kind") == "reminder" and (now - at) > STALE_AFTER:
            _retire(task)                     # 错过太久：直接作废，不补弹
            continue
        try:
            if task.get("kind") == "reminder":
                _fire_reminder(task, now)
            else:
                _fire_auto(task, now)
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
