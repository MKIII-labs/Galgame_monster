# -*- coding: utf-8 -*-
"""通用观测：把「做过什么、结果如何」记成带时间的事件，供模型按需查询。

不绑定任何领域：数学答题、聊天里的自述、工具反馈都写同一张表。
这里只负责存取与聚合，不判断用途——角色与目标由用户提示词定义。
"""
import json
import time
import uuid

from app import db

MAX_SUBJECT = 40
MAX_TOPIC = 60
MAX_VALUE = 2000
MAX_EVIDENCE = 600
DEFAULT_DAYS = 14
MAX_ROWS = 200
PRUNE_DAYS = 180
_PRUNE_EVERY = 3600
_last_prune = 0.0


def _clip(value, limit):
    return str(value if value is not None else "").strip()[:limit]


def _dumps(value):
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _loads(raw):
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _row(item, now):
    return (
        "o_" + uuid.uuid4().hex[:12],
        _clip(item.get("session_id"), 64),
        _clip(item.get("source"), 24) or "chat",
        _clip(item.get("subject"), MAX_SUBJECT),
        _clip(item.get("kind"), 24) or "attempt",
        _clip(item.get("topic"), MAX_TOPIC),
        _clip(_dumps(item.get("value")), MAX_VALUE) or "{}",
        _clip(item.get("evidence"), MAX_EVIDENCE),
        now,
    )


def record(*, subject="", topic="", value=None, evidence="", kind="attempt",
           source="chat", session_id=""):
    """写一条观测。失败返回 False，不抛异常（不能影响主流程）。"""
    ok = record_many([{"subject": subject, "topic": topic, "value": value,
                       "evidence": evidence, "kind": kind, "source": source,
                       "session_id": session_id}])
    return ok


def record_many(items):
    """批量写入（模拟卷一次几十题，用 executemany 少提交几次）。"""
    rows = [_row(it, time.time()) for it in (items or []) if isinstance(it, dict)]
    if not rows:
        return False
    try:
        conn = db.get_db()
        conn.executemany(
            "INSERT INTO observations"
            " (id, session_id, source, subject, kind, topic, value, evidence, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        _maybe_prune()
        return True
    except Exception as e:                       # noqa: BLE001
        print("[观测] 写入失败：%s" % e)
        return False


def recent(days=DEFAULT_DAYS, subject="", topic="", limit=MAX_ROWS):
    """按时间倒序取事件；subject / topic 是精确匹配。"""
    try:
        days = max(1, min(int(days or DEFAULT_DAYS), 365))
        limit = max(1, min(int(limit or MAX_ROWS), MAX_ROWS))
    except (TypeError, ValueError):
        days, limit = DEFAULT_DAYS, MAX_ROWS
    sql = ("SELECT id, session_id, source, subject, kind, topic, value, evidence, created_at"
           " FROM observations WHERE created_at >= ?")
    args = [time.time() - days * 86400]
    if subject:
        sql += " AND subject = ?"
        args.append(_clip(subject, MAX_SUBJECT))
    if topic:
        sql += " AND topic = ?"
        args.append(_clip(topic, MAX_TOPIC))
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    try:
        rows = db.get_db().execute(sql, args).fetchall()
    except Exception as e:                       # noqa: BLE001
        print("[观测] 查询失败：%s" % e)
        return []
    return [dict(r) for r in rows]


def query(days=DEFAULT_DAYS, subject="", topic="", limit=50):
    rows = recent(days=days, subject=subject, topic=topic, limit=limit)
    return {"days": days, "count": len(rows), "items": rows}


def summary(days=DEFAULT_DAYS, subject="", topic="", limit=MAX_ROWS):
    """按学科 / 主题聚合成结构化数据，供「学习观测」面板直接渲染。

    与 brief 的区别：brief 给模型看，压成几行文本；这里给界面用，保留分组与明细。
    """
    rows = recent(days=days, subject=subject, topic=topic, limit=limit)
    groups = {}
    for r in rows:
        key = (r.get("subject") or "", r.get("topic") or "")
        g = groups.setdefault(key, {"subject": key[0], "topic": key[1],
                                    "attempts": 0, "graded": 0, "correct": 0,
                                    "last": 0.0, "sources": {}})
        g["attempts"] += 1
        g["last"] = max(g["last"], float(r.get("created_at") or 0))
        src = r.get("source") or "chat"
        g["sources"][src] = g["sources"].get(src, 0) + 1
        data = _loads(r.get("value"))
        if isinstance(data.get("correct"), bool):
            g["graded"] += 1
            g["correct"] += 1 if data["correct"] else 0

    ordered = sorted(groups.values(), key=lambda g: (g["last"], g["attempts"]), reverse=True)
    for g in ordered:
        g["accuracy"] = round(100.0 * g["correct"] / g["graded"]) if g["graded"] else None

    graded = sum(g["graded"] for g in ordered)
    correct = sum(g["correct"] for g in ordered)
    return {"days": days, "count": len(rows),
            "subjects": len({g["subject"] for g in ordered if g["subject"]}),
            "graded": graded, "correct": correct,
            "accuracy": round(100.0 * correct / graded) if graded else None,
            "last": max((g["last"] for g in ordered), default=0.0),
            "groups": ordered, "items": rows}


def brief(query_text="", days=DEFAULT_DAYS, max_topics=3, max_chars=320):
    """压缩成给模型的几行摘要：按 subject/topic 聚合次数与正确率。

    只给事实，不下结论；样本少于 3 次时明确标注，避免模型硬编。
    """
    rows = recent(days=days, limit=MAX_ROWS)
    if not rows:
        return ""
    groups = {}
    for r in rows:
        key = (r.get("subject") or "", r.get("topic") or "")
        g = groups.setdefault(key, {"subject": key[0], "topic": key[1],
                                    "attempts": 0, "graded": 0, "correct": 0,
                                    "last": 0.0})
        g["attempts"] += 1
        g["last"] = max(g["last"], float(r.get("created_at") or 0))
        data = _loads(r.get("value"))
        if isinstance(data.get("correct"), bool):
            g["graded"] += 1
            g["correct"] += 1 if data["correct"] else 0

    q = str(query_text or "")

    def rank(g):
        hit = 1 if q and ((g["subject"] and g["subject"] in q)
                          or (g["topic"] and g["topic"] in q)) else 0
        return (hit, g["attempts"], g["last"])

    items = sorted(groups.values(), key=rank, reverse=True)
    lines = ["【相关观测 · 近 %d 天】" % max(1, int(days or DEFAULT_DAYS))]
    for g in items[:max(1, int(max_topics or 3))]:
        name = " / ".join(x for x in (g["subject"], g["topic"]) if x) or "未分类"
        if g["graded"]:
            rate = round(100.0 * g["correct"] / max(1, g["graded"]))
            lines.append("%s：%d 次，对 %d（%d%%）" % (name, g["attempts"], g["correct"], rate))
        else:
            lines.append("%s：%d 条记录" % (name, g["attempts"]))
    text = "\n".join(lines)
    if sum(g["graded"] for g in items) < 3:
        text += "\n（样本少于 3 次，结论仅供参考）"
    return text[:max_chars]


def prune(days=PRUNE_DAYS):
    """清理过期观测。失败静默。"""
    try:
        cutoff = time.time() - max(1, int(days or PRUNE_DAYS)) * 86400
        conn = db.get_db()
        conn.execute("DELETE FROM observations WHERE created_at < ?", (cutoff,))
        conn.commit()
        return True
    except Exception as e:                       # noqa: BLE001
        print("[观测] 清理失败：%s" % e)
        return False


def _maybe_prune():
    """每小时最多清理一次，避免每次写入都扫表。"""
    global _last_prune
    now = time.time()
    if now - _last_prune < _PRUNE_EVERY:
        return
    _last_prune = now
    prune()
