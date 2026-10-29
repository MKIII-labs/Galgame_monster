# -*- coding: utf-8 -*-
"""研究评测 CLI。

用法：
    python scripts/eval_research.py --validate
    python scripts/eval_research.py --list
    python scripts/eval_research.py --session s_xxx --task mc-core
    python scripts/eval_research.py --session s_xxx --json
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import research_eval, sessions, session_events  # noqa: E402


def _find_task(tasks, tid):
    return next((t for t in tasks if t.get("id") == tid), None)


def main():
    ap = argparse.ArgumentParser(description="研究任务评测（机检）")
    ap.add_argument("--validate", action="store_true", help="校验评测集结构")
    ap.add_argument("--list", action="store_true", help="列出任务")
    ap.add_argument("--session", help="会话 id（读 data/sessions.json）")
    ap.add_argument("--task", help="任务 id；省略时用第一条 replicate 任务")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    data = research_eval.load_tasks()
    errs = research_eval.validate(data)
    if args.validate:
        if errs:
            print("\n".join(errs))
            return 1
        print("评测集合法：%d 条任务" % len(data.get("tasks") or []))
        return 0
    if errs:
        print("\n".join(errs))
        return 1

    tasks = data.get("tasks") or []
    if args.list:
        for t in tasks:
            print("%-16s %-10s must_search=%d  %s" % (
                t.get("id"), t.get("kind"), int(t.get("must_search") or 0),
                str(t.get("prompt") or "")[:50]))
        return 0
    if not args.session:
        ap.print_help()
        return 0

    sess = sessions.find_session(sessions.load_sessions(), args.session)
    if not sess:
        # sessions.json 里没有时，从 events.jsonl 重建（session_store=events 的常态）
        sess = session_events.rebuild(args.session)
    if not sess:
        print("找不到会话：%s" % args.session)
        return 1
    task = _find_task(tasks, args.task) if args.task else next(
        (t for t in tasks if t.get("kind") == "replicate"), tasks[0])
    if not task:
        print("找不到任务：%s" % args.task)
        return 1
    result = research_eval.evaluate(task, sess.get("messages") or [])
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(research_eval.format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
