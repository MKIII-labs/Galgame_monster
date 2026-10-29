# -*- coding: utf-8 -*-
"""观测能力：把通用观测层接到对话与任务引擎。

- `observe.record`：写一条观测（做过什么、结果如何），对话里可直接调用；
- `observe.query`：查近期观测并按学科 / 主题聚合，只读。
"""
import json
import time

from app import capabilities, observe


def _parse_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {"note": value}
    return value if value is not None else {}


def _record(args, ctx):
    subject = str(args.get("subject") or "").strip()
    topic = str(args.get("topic") or "").strip()
    if not (subject or topic):
        return "❌ 至少需要 subject 或 topic。"
    ok = observe.record(
        subject=subject, topic=topic,
        value=_parse_value(args.get("value")),
        evidence=str(args.get("evidence") or ""),
        kind=str(args.get("kind") or "note"),
        source="chat",
        session_id=(ctx or {}).get("session_id") or "")
    if not ok:
        return "❌ 记录失败。"
    return "已记录：%s / %s" % (subject or "未分类", topic or "未分类")


def _query(args, ctx):
    try:
        days = int(args.get("days") or observe.DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = observe.DEFAULT_DAYS
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    out = observe.query(days=days,
                        subject=str(args.get("subject") or "").strip(),
                        topic=str(args.get("topic") or "").strip(),
                        limit=limit)
    if not out["count"]:
        return "近 %d 天没有观测记录。" % out["days"]
    lines = ["近 %d 天共 %d 条：" % (out["days"], out["count"])]
    for r in out["items"][:20]:
        day = time.strftime("%m-%d", time.localtime(float(r.get("created_at") or 0)))
        name = " / ".join(x for x in (r.get("subject"), r.get("topic")) if x) or "未分类"
        data = _parse_value(r.get("value"))
        mark = ""
        if isinstance(data.get("correct"), bool):
            mark = " ✓" if data["correct"] else " ✗"
        line = "- [%s] %s%s" % (day, name, mark)
        evidence = str(r.get("evidence") or "").replace("\n", " ").strip()
        if evidence:
            line += "：" + evidence[:240]
        lines.append(line)
    return "\n".join(lines)


capabilities.capability(
    "observe.record",
    "记录一条观测：做过什么、结果如何；供之后回顾与归因",
    {"subject": "str", "topic": "str", "value": "str?",
     "evidence": "str?", "kind": "str?"},
    permission="write")(_record)
capabilities.capability(
    "observe.query",
    "查询近期观测记录，可按学科 / 主题过滤",
    {"subject": "str?", "topic": "str?", "days": "int?", "limit": "int?"})(_query)
