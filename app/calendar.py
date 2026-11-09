# -*- coding: utf-8 -*-
"""个人日程：事件 CRUD、重复规则展开、冲突检测、到点提醒。

设计取舍：
- 时间统一用 **epoch 秒**（REAL）存储，避免时区字符串的歧义；
- 重复规则支持 RFC5545 的常用子集：
  ``FREQ=DAILY|WEEKLY|MONTHLY``、``INTERVAL=n``、``BYDAY=MO,WE``、``BYMONTHDAY=15``；
  也接受 ``daily`` / ``weekly`` / ``monthly`` 简写；
- 不引入第三方日期库：规则展开器只覆盖个人日程真正会用到的部分，
  遇到不认识的 FREQ 直接按「不重复」处理，且不会抛异常。
"""
import datetime
import re
import time
import uuid

from app import events as events_bus
from app import scheduler
from app.capabilities import capability
from app.db import get_db

_WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_MAX_OCCURRENCES = 500
_TIME_FMT = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M",
             "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


# ------------------------------------------------------------
# 时间
# ------------------------------------------------------------
def parse_time(v):
    """把 epoch 或常见字符串解析成 epoch 秒；失败返回 None"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = str(v or "").strip()
    if not s:
        return None
    if s.replace(".", "", 1).isdigit():
        try:
            return float(s)
        except ValueError:
            return None
    for fmt in _TIME_FMT:
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def fmt_time(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return "?"


# ------------------------------------------------------------
# 自然语言快速添加（工作台「快速添加」输入框）
# ------------------------------------------------------------
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_int(s):
    """把「十」「十五」「二十」这类中文数字转成 int，失败返回 -1。"""
    s = str(s or "").strip()
    if not s:
        return -1
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, b = s.split("十", 1)
        tens = _CN_DIGITS.get(a, 1) if a else 1
        ones = _CN_DIGITS.get(b, 0) if b else 0
        return tens * 10 + ones
    if len(s) == 1 and s in _CN_DIGITS:
        return _CN_DIGITS[s]
    return -1


def parse_natural_time(text, now=None):
    """从一句话里抽出时间，返回 (start_ts, title)；没识别出时间返回 (None, 原文)。

    只覆盖个人日程真正会用到的写法：
    「明天 10 点 开会」「周四 15:30 交报告」「10月25日 9:00 体检」「下午 3 点 面试」
    """
    raw = str(text or "").strip()
    if not raw:
        return None, ""
    now = float(now or time.time())
    base = datetime.datetime.fromtimestamp(now)
    day = None
    weekday_given = False
    hour = minute = None
    rest = raw

    # 上午 / 下午 先摘出来，避免和数字混在一起
    ampm = ""
    m = re.search(r"(下午|晚上|傍晚|夜里|中午)", rest)
    if m:
        ampm = "pm"
        rest = rest[:m.start()] + " " + rest[m.end():]
    else:
        m = re.search(r"(上午|早上|早晨|凌晨)", rest)
        if m:
            ampm = "am"
            rest = rest[:m.start()] + " " + rest[m.end():]

    # 相对日期（长词优先）
    for word, delta in (("大后天", 3), ("后天", 2), ("明天", 1),
                        ("今天", 0), ("今晚", 0)):
        if word in rest:
            rest = rest.replace(word, " ", 1)
            day = (base + datetime.timedelta(days=delta)).date()
            break

    # 周X / 下周X
    if day is None:
        m = re.search(r"(下)?(?:周|星期|礼拜)([一二三四五六日天1-7])", rest)
        if m:
            rest = rest[:m.start()] + " " + rest[m.end():]
            ch = m.group(2)
            idx = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
                   "日": 6, "天": 6}.get(ch)
            if idx is None:
                idx = int(ch) - 1
            delta = (idx - base.weekday()) % 7
            if m.group(1):                 # 「下周X」= 再往后一周
                delta += 7
            day = (base + datetime.timedelta(days=delta)).date()
            weekday_given = True

    # M月D日 / D号
    if day is None:
        m = re.search(r"(\d{1,2}|[一二三四五六七八九十]+)\s*月\s*"
                      r"(\d{1,2}|[一二三四五六七八九十]+)\s*[日号]?", rest)
        if m:
            rest = rest[:m.start()] + " " + rest[m.end():]
            mon, dd = _cn_int(m.group(1)), _cn_int(m.group(2))
            if 1 <= mon <= 12 and 1 <= dd <= 31:
                try:
                    day = datetime.date(base.year, mon, dd)
                except ValueError:
                    day = None

    # HH:MM
    m = re.search(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)", rest)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        rest = rest[:m.start()] + " " + rest[m.end():]
    else:
        m = re.search(r"(?<!\d)([0-2]?\d|[一二三四五六七八九十]+)\s*[点時时]"
                      r"(半|[0-5]?\d\s*分?)?", rest)
        if m:
            hour = _cn_int(m.group(1))
            mm = (m.group(2) or "").replace(" ", "")
            if mm.startswith("半"):
                minute = 30
            elif mm:
                minute = int(re.sub(r"\D", "", mm) or 0)
            else:
                minute = 0
            rest = rest[:m.start()] + " " + rest[m.end():]

    if hour is not None:
        if not (0 <= hour <= 23) or minute is None or not (0 <= minute <= 59):
            hour = minute = None
        elif ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

    if day is None and hour is None:
        return None, raw
    if hour is None:
        hour, minute = 9, 0

    if day is None:
        cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand.timestamp() <= now + 60:
            cand += datetime.timedelta(days=1)
    else:
        cand = datetime.datetime.combine(day, datetime.time(hour, minute))
        if weekday_given and cand.timestamp() <= now + 60:
            cand += datetime.timedelta(days=7)

    title = re.sub(r"\s+", " ", rest).strip()
    title = re.sub(r"^[\s,，。;；:：\-—]+", "", title)
    title = re.sub(r"^(在|于)\s*", "", title)
    if not title:
        title = "新日程"
    return cand.timestamp(), title


def create_quick_event(text, now=None):
    """按自然语言创建一条 1 小时的日程（默认提前 10 分钟提醒）。"""
    start, title = parse_natural_time(text, now=now)
    if start is None:
        raise ValueError("没识别出时间，试试「明天 10 点 开会」")
    return create_event(title, start, start + 3600, remind_before=600)


# ------------------------------------------------------------
# 重复规则
# ------------------------------------------------------------
def _parse_rrule(raw):
    s = (raw or "").strip()
    if not s:
        return None
    low = s.lower()
    if low in ("daily", "weekly", "monthly"):
        s = "FREQ=" + low.upper()
    out = {"freq": "", "interval": 1, "byday": [], "bymonthday": []}
    for part in s.upper().split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "FREQ":
            out["freq"] = v
        elif k == "INTERVAL":
            try:
                out["interval"] = max(1, int(v))
            except ValueError:
                out["interval"] = 1
        elif k == "BYDAY":
            out["byday"] = [_WEEKDAY[x] for x in v.split(",") if x in _WEEKDAY]
        elif k == "BYMONTHDAY":
            for x in v.split(","):
                try:
                    d = int(x)
                except ValueError:
                    continue
                if 1 <= d <= 31:
                    out["bymonthday"].append(d)
    if out["freq"] not in ("DAILY", "WEEKLY", "MONTHLY"):
        return None
    return out


def _add_months(y, m, n):
    total = y * 12 + (m - 1) + n
    return total // 12, total % 12 + 1


def _iter_starts(dtstart, rule, range_start, range_end):
    """生成 [range_start, range_end) 内的重复起始时间"""
    freq = rule["freq"]
    step = rule["interval"]
    out = []

    if freq == "DAILY":
        cur = dtstart
        # range_start 是 float 时间戳，cur 是 datetime；先统一成时间戳再比较/相减，
        # 否则有 FREQ=DAILY 日程时整张日程表都会 TypeError（工作台直接加载失败）。
        if range_start > cur.timestamp():
            days = int((range_start - cur.timestamp()) // 86400)
            cur += datetime.timedelta(days=max(0, days // step) * step)
        while cur.timestamp() < range_end and len(out) < _MAX_OCCURRENCES:
            ts = cur.timestamp()
            if ts >= range_start:
                out.append(ts)
            cur += datetime.timedelta(days=step)
        return out

    if freq == "WEEKLY":
        byday = rule["byday"] or [dtstart.weekday()]
        anchor = dtstart - datetime.timedelta(days=dtstart.weekday())   # 本周一
        weeks = 0
        if range_start > anchor.timestamp():
            weeks = int((range_start - anchor.timestamp()) // (7 * 86400))
            weeks = max(0, weeks // step) * step
        cur = anchor + datetime.timedelta(weeks=weeks)
        while cur.timestamp() < range_end and len(out) < _MAX_OCCURRENCES:
            for wd in byday:
                d = cur + datetime.timedelta(days=wd)
                ts = d.timestamp()
                if ts < dtstart.timestamp():
                    continue
                if range_start <= ts < range_end:
                    out.append(ts)
            cur += datetime.timedelta(weeks=step)
        return sorted(set(out))

    # MONTHLY
    day = rule["bymonthday"][0] if rule["bymonthday"] else dtstart.day
    y, m = dtstart.year, dtstart.month
    diff = (datetime.datetime.fromtimestamp(range_start).year - y) * 12 \
        + (datetime.datetime.fromtimestamp(range_start).month - m)
    if diff > 0:
        y, m = _add_months(y, m, max(0, diff // step) * step)
    while len(out) < _MAX_OCCURRENCES:
        try:
            d = datetime.datetime(y, m, day, dtstart.hour, dtstart.minute,
                                  dtstart.second)
        except ValueError:
            d = None
        if d is not None:
            ts = d.timestamp()
            if ts >= range_end:
                break
            if ts >= range_start and ts >= dtstart.timestamp():
                out.append(ts)
        y, m = _add_months(y, m, step)
        if (y, m) > (datetime.datetime.fromtimestamp(range_end).year,
                     datetime.datetime.fromtimestamp(range_end).month):
            break
    return out


# ------------------------------------------------------------
# 存取
# ------------------------------------------------------------
def _row(r):
    return dict(r) if r is not None else None


def get_event(eid):
    cur = get_db().execute("SELECT * FROM events WHERE id=?", (str(eid or ""),))
    return _row(cur.fetchone())


def _exdates(eid):
    cur = get_db().execute(
        "SELECT occurrence_at FROM event_exdates WHERE event_id=?", (eid,))
    return {float(r["occurrence_at"]) for r in cur.fetchall()}


def _occurrences(ev, range_start, range_end):
    """返回事件在区间内的起始时间列表（已排除 EXDATE）"""
    start = float(ev["start_at"])
    end = float(ev["end_at"] or start)
    duration = max(1.0, end - start)
    rule = _parse_rrule(ev.get("rrule"))
    if rule:
        starts = _iter_starts(datetime.datetime.fromtimestamp(start), rule,
                              range_start, range_end)
    else:
        starts = [start] if start < range_end else []
    ex = _exdates(ev["id"])
    return [s for s in starts if s not in ex and s + duration > range_start]


def list_events(range_start, range_end):
    """列出区间内的日程（重复事件会展开成多条 occurrence）"""
    if range_end <= range_start:
        return []
    cur = get_db().execute(
        "SELECT * FROM events WHERE status='active' AND start_at < ? "
        "ORDER BY start_at", (range_end,))
    out = []
    for row in cur.fetchall():
        ev = dict(row)
        duration = max(1.0, float(ev["end_at"] or ev["start_at"]) - float(ev["start_at"]))
        for occ in _occurrences(ev, range_start, range_end):
            item = dict(ev)
            item["occurrence_at"] = occ
            item["occurrence_end"] = occ + duration
            out.append(item)
    out.sort(key=lambda x: x["occurrence_at"])
    return out


def find_conflicts(start_at, end_at, exclude_id=""):
    """返回与 [start_at, end_at) 重叠的日程（最多 5 条）"""
    items = list_events(start_at - 7 * 86400, end_at + 7 * 86400)
    hits = []
    for it in items:
        if exclude_id and it["id"] == exclude_id:
            continue
        if it["occurrence_at"] < end_at and it["occurrence_end"] > start_at:
            hits.append(it)
            if len(hits) >= 5:
                break
    return hits


def create_event(title, start_at, end_at=None, all_day=False, location="",
                 notes="", rrule="", remind_before=0, check_conflict=True,
                 source="calendar", source_id="", session_id=""):
    title = str(title or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    start = parse_time(start_at)
    if start is None:
        raise ValueError("开始时间无法识别")
    end = parse_time(end_at) if end_at else None
    if end is None:
        end = start + (86400 if all_day else 3600)
    if end <= start:
        raise ValueError("结束时间必须晚于开始时间")

    conflict = find_conflicts(start, end) if check_conflict else []
    eid = "e_" + uuid.uuid4().hex[:12]
    now = time.time()
    conn = get_db()
    conn.execute(
        "INSERT INTO events (id,title,notes,start_at,end_at,all_day,location,"
        "rrule,remind_before,status,source,source_id,session_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?, 'active', ?,?,?, ?,?)",
        (eid, title, str(notes or ""), start, end, 1 if all_day else 0,
         str(location or ""), str(rrule or ""), max(0, int(remind_before or 0)),
         str(source or "calendar"), str(source_id or ""), str(session_id or ""),
         now, now))
    conn.commit()
    ev = get_event(eid)
    ev["_conflicts"] = [{"id": c["id"], "title": c["title"],
                         "start_at": c["occurrence_at"]} for c in conflict]
    events_bus.publish({"type": "calendar_update"})
    return ev


def _next_occurrence(ev, after):
    occ = _occurrences(ev, float(after) + 1, float(after) + 370 * 86400)
    return occ[0] if occ else 0


def _sync_legacy_notify(ev):
    """旧提醒迁移后仍保留「程序关着也能弹」的 Windows 计划任务。"""
    try:
        from app import notify
        if not notify.available():
            return
        key = ev.get("source_id") or ev["id"]
        system_on = (scheduler.switch_on("reminders_enabled")
                     and scheduler.switch_on("reminder_system_enabled"))
        if not system_on:
            notify.unregister(key)
            notify.cleanup_payload(key)
            return
        when = _next_occurrence(ev, time.time())
        if not when:
            notify.unregister(key)
            notify.cleanup_payload(key)
            return
        notify.register(key, datetime.datetime.fromtimestamp(when),
                        ev.get("title") or "提醒", ev.get("notes") or "")
    except Exception as e:
        print("[日程] 同步系统提醒失败：%s" % e)


def apply_system_switch():
    """提醒总开关 / 系统通知开关变化后，同步旧提醒的 Windows 计划任务。

    新提醒（source != legacy_reminder）本来就不注册系统任务，由调度器在程序
    运行时处理；这里只需要管迁移过来的旧提醒。
    """
    from app import notify
    on = (scheduler.switch_on("reminders_enabled")
          and scheduler.switch_on("reminder_system_enabled"))
    try:
        rows = get_db().execute(
            "SELECT * FROM events WHERE status='active' AND source='legacy_reminder'"
        ).fetchall()
    except Exception as e:               # noqa: BLE001
        print("[日程] 读取旧提醒失败：%s" % e)
        return
    for row in rows:
        ev = dict(row)
        if on:
            _sync_legacy_notify(ev)
        else:
            key = ev.get("source_id") or ev["id"]
            try:
                notify.unregister(key)
                notify.cleanup_payload(key)
            except Exception:            # noqa: BLE001
                pass


def update_event(eid, **fields):
    ev = get_event(eid)
    if not ev:
        return None
    allowed = {"title", "notes", "start_at", "end_at", "all_day", "location",
               "rrule", "remind_before", "status"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("start_at", "end_at"):
            v = parse_time(v)
            if v is None:
                continue
        elif k == "all_day":
            v = 1 if v else 0
        elif k == "remind_before":
            v = max(0, int(v or 0))
        elif k in ("title", "notes", "location", "rrule", "status"):
            v = str(v or "")
        sets.append("%s=?" % k)
        vals.append(v)
    if not sets:
        return ev
    vals.append(time.time())
    vals.append(eid)
    conn = get_db()
    conn.execute("UPDATE events SET %s, updated_at=? WHERE id=?" % ", ".join(sets),
                 tuple(vals))
    conn.commit()
    ev2 = get_event(eid)
    if ev2 and ev2.get("source") == "legacy_reminder":
        _sync_legacy_notify(ev2)
    events_bus.publish({"type": "calendar_update"})
    return ev2


def delete_event(eid):
    ev = get_event(eid)
    conn = get_db()
    cur = conn.execute("DELETE FROM events WHERE id=?", (str(eid or ""),))
    conn.commit()
    if ev and ev.get("source") == "legacy_reminder":
        try:
            from app import notify
            key = ev.get("source_id") or ev["id"]
            notify.unregister(key)
            notify.cleanup_payload(key)
        except Exception:
            pass
    if cur.rowcount > 0:
        events_bus.publish({"type": "calendar_update"})
    return cur.rowcount > 0


# ------------------------------------------------------------
# 任务截止日程
# ------------------------------------------------------------
def bind_task_event(task_id, title, due_at, remind_before=600, session_id="", notes=""):
    """给任务建一条截止日程：提醒复用 due_reminders，不另起一套调度。"""
    start = parse_time(due_at)
    if start is None:
        return None
    return create_event(
        "任务截止：" + str(title or "").strip(),
        start, start + 60,
        notes=str(notes or ("任务：%s" % (title or ""))),
        remind_before=max(0, int(remind_before or 0)),
        source="task", source_id=str(task_id or ""),
        session_id=str(session_id or ""),
        check_conflict=False)


def close_task_event(task_id):
    """任务完成 / 取消后关掉对应日程，避免到期还提醒。返回影响条数。"""
    conn = get_db()
    cur = conn.execute(
        "UPDATE events SET status='done', updated_at=? "
        "WHERE source='task' AND source_id=? AND status='active'",
        (time.time(), str(task_id or "")))
    conn.commit()
    return cur.rowcount


def delete_task_event(task_id):
    """删除任务时连带删掉它的截止日程；返回删除条数。"""
    rows = get_db().execute(
        "SELECT id FROM events WHERE source='task' AND source_id=?",
        (str(task_id or ""),)).fetchall()
    for row in rows:
        delete_event(row["id"])
    return len(rows)


def purge_expired(before=None):
    """手动清理：删除已经结束的一次性日程（重复日程保留）。"""
    cutoff = parse_time(before) if str(before or "").strip() else time.time()
    if cutoff is None:
        cutoff = time.time()
    rows = get_db().execute(
        "SELECT id FROM events WHERE rrule='' AND end_at <= ?", (cutoff,)).fetchall()
    for row in rows:
        delete_event(row["id"])
    return len(rows)


# ------------------------------------------------------------
# 到点提醒
# ------------------------------------------------------------
def due_reminders(now=None):
    """由调度器每 15 秒调用一次：找出该提醒且还没提醒过的日程。

    提醒窗口：``occurrence - remind_before <= now < occurrence``。
    程序关着时错过的提醒不补（与一次性提醒的 STALE_AFTER 策略一致）。
    """
    if not scheduler.switch_on("reminders_enabled") \
            or not scheduler.switch_on("reminder_calendar_enabled"):
        return []                        # 总开关 / 日程提醒关闭：不触发，也不落已提醒记录
    inapp = scheduler.switch_on("reminder_inapp_enabled")
    now = float(now or time.time())
    cur = get_db().execute(
        "SELECT * FROM events WHERE status='active' AND "
        "(remind_before > 0 OR source='legacy_reminder' OR session_id != '')")
    fired = []
    for row in cur.fetchall():
        ev = dict(row)
        is_legacy = ev.get("source") == "legacy_reminder"
        before = int(ev.get("remind_before") or 0)
        for occ in _occurrences(ev, now - 86400, now + 86400):
            if before > 0 and not is_legacy:
                if not (occ - before <= now < occ):
                    continue
            else:
                # 没有提前量（旧提醒 / 设置页新建的提醒）：在到点后的 15 分钟内触发，
                # 给 15 秒一次的调度扫描留出窗口。
                if not (occ <= now < occ + 900):
                    continue
            conn = get_db()
            ins = conn.execute(
                "INSERT OR IGNORE INTO event_fired (event_id,occurrence_at,fired_at) "
                "VALUES (?,?,?)", (ev["id"], occ, now))
            conn.commit()
            if ins.rowcount <= 0:
                continue

            if is_legacy:
                # 旧提醒：保持原来的行为——会话里留一条 + 推送通知；
                # 一次性提醒结束后标记 done，重复提醒重新注册下一次系统任务。
                body = (ev.get("notes") or "").strip() or "时间到了"
                sid = ev.get("session_id") or ""
                if inapp and sid:
                    try:
                        from app import scheduler as sched
                        sched._append_message(sid, "⏰ **提醒**：" + body)
                    except Exception as e:
                        print("[日程] 追加提醒消息失败：%s" % e)
                if inapp:
                    events_bus.publish({"type": "notify", "kind": "reminder",
                                        "session_id": sid,
                                        "task_id": ev.get("source_id") or ev["id"],
                                        "title": ev.get("title") or "提醒",
                                        "text": body})
                text = body
                if ev.get("rrule"):
                    _sync_legacy_notify(ev)
                else:
                    update_event(ev["id"], status="done")
            else:
                text = "%s（%s）" % (ev["title"], fmt_time(occ))
                if ev.get("location"):
                    text += " · " + ev["location"]
                if inapp:
                    events_bus.publish({"type": "notify", "kind": "calendar",
                                        "session_id": ev.get("session_id") or "",
                                        "title": "日程提醒", "text": text})
                    if ev.get("session_id"):
                        try:
                            from app import scheduler as sched
                            sched._append_message(ev["session_id"],
                                                  "⏰ **日程提醒**：" + text)
                        except Exception as e:
                            print("[日程] 追加日程消息失败：%s" % e)

            try:
                from app import workflow
                workflow.emit("calendar.due", {"event_id": ev["id"],
                                               "title": ev["title"], "text": text})
            except Exception as e:
                print("[日程] 触发工作流失败：%s" % e)
            fired.append({"event_id": ev["id"], "occurrence_at": occ,
                          "title": ev["title"], "text": text})
    return fired


# ------------------------------------------------------------
# 旧提醒迁移（把 tasks.json 的 reminder 并入 events）
# ------------------------------------------------------------
def migrate_legacy_reminders():
    """把旧的单次/每天/每周提醒复制成 events，并从 tasks.json 移除。

    - 只迁移「启用且未暂停」的提醒；停用的留在原设置页，启用后下次启动会被迁移。
    - 不移除 Windows 计划任务（remove_task(..., unregister=False)），
      所以「程序关着也能弹」的能力保持不变。
    - 幂等：同一 source_id 已迁移过就跳过。
    """
    from app import scheduler as sched
    moved = 0
    now = time.time()
    for t in sched.list_tasks():
        if t.get("kind") != "reminder" or not t.get("enabled") or t.get("paused"):
            continue
        sid = str(t.get("id") or "")
        at = float(t.get("at") or 0)
        if not sid or at <= 0:
            continue
        exists = get_db().execute(
            "SELECT 1 FROM events WHERE source='legacy_reminder' AND source_id=? LIMIT 1",
            (sid,)).fetchone()
        if exists:
            continue
        repeat = str(t.get("repeat") or "")
        rrule = {"daily": "FREQ=DAILY", "weekly": "FREQ=WEEKLY"}.get(repeat, "")
        if not rrule and at <= now - 60:
            continue                       # 已经过期的一次性提醒交给原调度器自行作废
        try:
            create_event(t.get("title") or "提醒", at, at + 60,
                         notes=t.get("text") or "", rrule=rrule,
                         remind_before=0, check_conflict=False,
                         source="legacy_reminder", source_id=sid,
                         session_id=t.get("session_id") or "")
            sched.remove_task(sid, unregister=False)
            moved += 1
        except Exception as e:
            print("[日程] 迁移旧提醒 %s 失败：%s" % (sid, e))
    if moved:
        print("[日程] 已把 %d 条旧提醒并入工作台" % moved)
    return moved


# ------------------------------------------------------------
# 能力（给对话 Agent 与任务引擎共用）
# ------------------------------------------------------------
def _cap_create(args, ctx):
    ev = create_event(
        args.get("title"), args.get("start_at"), args.get("end_at"),
        all_day=bool(args.get("all_day")), location=args.get("location") or "",
        notes=args.get("notes") or "", rrule=args.get("rrule") or "",
        remind_before=args.get("remind_before") or 0,
        session_id=(ctx or {}).get("session_id") or "",
        check_conflict=not bool(args.get("skip_conflict_check")))
    text = "已创建日程 %s：%s（%s → %s）" % (
        ev["id"], ev["title"], fmt_time(ev["start_at"]), fmt_time(ev["end_at"]))
    if ev.get("_conflicts"):
        text += "\n⚠ 与以下日程时间重叠：" + "；".join(
            "%s %s" % (c["title"], fmt_time(c["start_at"])) for c in ev["_conflicts"])
    if ev["rrule"]:
        text += "\n重复规则：%s" % ev["rrule"]
    return text


def _cap_list(args, ctx):
    start = parse_time(args.get("start_at"))
    end = parse_time(args.get("end_at"))
    now = time.time()
    if start is None:
        start = datetime.datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
    if end is None:
        end = start + 7 * 86400
    items = list_events(start, end)
    if not items:
        return "这段时间没有日程。"
    lines = ["共 %d 条日程：" % len(items)]
    for it in items[:50]:
        line = "- %s  %s  [%s]" % (fmt_time(it["occurrence_at"]), it["title"], it["id"])
        if it.get("location"):
            line += "  @" + it["location"]
        if it.get("rrule"):
            line += "  (重复)"
        lines.append(line)
    return "\n".join(lines)


def _cap_update(args, ctx):
    eid = str(args.get("id") or "")
    fields = {k: v for k, v in args.items() if k != "id"}
    ev = update_event(eid, **fields)
    if not ev:
        return "❌ 找不到日程：%s" % eid
    return "已更新日程 %s：%s（%s）" % (ev["id"], ev["title"], fmt_time(ev["start_at"]))


def _cap_delete(args, ctx):
    eid = str(args.get("id") or "")
    return ("已删除日程 %s" % eid) if delete_event(eid) else ("❌ 找不到日程：%s" % eid)


capability("calendar.create", "新建日程；时间用 YYYY-MM-DD HH:MM 或 epoch 秒",
           {"title": "str", "start_at": "time", "end_at": "time?",
            "all_day": "bool?", "location": "str?", "notes": "str?",
            "rrule": "str?", "remind_before": "秒?"}, permission="write")(_cap_create)
capability("calendar.list", "查询一段时间的日程（默认今天起 7 天）",
           {"start_at": "time?", "end_at": "time?"}, permission="read")(_cap_list)
capability("calendar.update", "修改日程，需要 id",
           {"id": "str", "title": "str?", "start_at": "time?",
            "end_at": "time?", "location": "str?", "notes": "str?",
            "rrule": "str?", "remind_before": "秒?"}, permission="write")(_cap_update)
capability("calendar.delete", "删除日程，需要 id",
           {"id": "str"}, permission="write")(_cap_delete)


# 把「到点提醒」挂到现有调度器上：调度器不需要认识日历，只按接口回调。
scheduler.add_source(due_reminders)
