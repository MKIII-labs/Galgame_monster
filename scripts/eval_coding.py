# -*- coding: utf-8 -*-
"""编码评测集 CLI。

用法：
    python scripts/eval_coding.py                 # mock：参考解 + 判卷，自检题目，零 API 成本
    python scripts/eval_coding.py --live          # 真实 Agent 跑一遍（消耗 token）
    python scripts/eval_coding.py --live --task fix-add --max-rounds 6
    python scripts/eval_coding.py --out data/eval_coding.json

输出：每题 PASS/FAIL + 原因；结果写入 --out（默认 data/eval_coding.json）。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import eval_coding as ec  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="编码评测集")
    ap.add_argument("--tasks", default=ec.TASKS_DIR, help="任务目录")
    ap.add_argument("--task", action="append", default=None, help="只跑指定任务（可多次）")
    ap.add_argument("--live", action="store_true", help="用真实 Agent 跑（消耗 token）")
    ap.add_argument("--max-rounds", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "eval_coding.json"))
    args = ap.parse_args()

    try:
        tasks = ec.load_tasks(args.tasks, only=args.task)
    except ValueError as e:
        print("任务加载失败：%s" % e)
        return 2
    if not tasks:
        print("没有找到任务（%s）" % args.tasks)
        return 2

    mode = "live" if args.live else "mock"
    print("编码评测 · %s · %d 题\n" % (mode, len(tasks)))
    started = time.time()
    results = []
    for task in tasks:
        print("- %-22s " % task.get("name"), end="", flush=True)
        t0 = time.time()
        try:
            if args.live:
                res = ec.run_live(task, max_rounds=args.max_rounds,
                                  timeout=args.timeout)
            else:
                res = ec.run_mock(task)
        except Exception as e:                     # noqa: BLE001
            res = {"name": task.get("name"), "mode": mode, "ok": False,
                   "reasons": ["评测异常：%s" % e], "changed": [], "deleted": [],
                   "metrics": {}}
        res["ms"] = int((time.time() - t0) * 1000)
        results.append(res)
        print("PASS" if res.get("ok") else "FAIL")
        for reason in res.get("reasons") or []:
            print("    · " + str(reason))
        m = res.get("metrics") or {}
        if m:
            bits = []
            if m.get("rounds") is not None:
                bits.append("轮数 %s" % m.get("rounds"))
            if m.get("tool_calls") is not None:
                bits.append("工具 %s" % m.get("tool_calls"))
            if m.get("tool_errors"):
                bits.append("工具错误 %s" % m.get("tool_errors"))
            if m.get("tokens"):
                bits.append("tokens %s" % m.get("tokens"))
            if m.get("verified") is not None:
                bits.append("跑过命令 %s" % ("是" if m.get("verified") else "否"))
            if bits:
                print("    · " + " · ".join(bits))

    passed = sum(1 for r in results if r.get("ok"))
    total = len(results)
    print("\n结果：%d/%d 通过，用时 %.1fs" % (passed, total, time.time() - started))
    payload = {
        "mode": mode,
        "ts": time.time(),
        "passed": passed,
        "total": total,
        "results": results,
    }
    try:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("已写入：%s" % args.out)
    except OSError as e:
        print("结果写入失败：%s" % e)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
