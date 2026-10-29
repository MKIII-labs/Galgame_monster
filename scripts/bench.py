# -*- coding: utf-8 -*-
"""评测跑分 CLI。

用法：
    python scripts/bench.py list
    python scripts/bench.py dry-run
    python scripts/bench.py run --only bug_fix,refactor --model <model>
    python scripts/bench.py run --ablate no_visual,no_assets
    python scripts/bench.py report --run data/bench/runs/20261026-120000
    python scripts/bench.py review --a <runA> --b <runB> --out data/bench/review.html

说明：
- `run` 会真实调用模型并消耗 token；`dry-run` 不调用。
- `--ablate a,b` 表示分别跑「基线」与「单个消融」，不是叠加。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import bench  # noqa: E402


def _ids(raw):
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def cmd_list(_args):
    for t in bench.load_tasks():
        print("%-14s %-8s %s" % (t.get("id"), t.get("difficulty") or "-",
                                 t.get("title") or ""))
    return 0


def cmd_dry(_args):
    rows = bench.dry_run()
    bad = 0
    for r in rows:
        mark = "OK " if r["ok"] else "ERR"
        print("[%s] %s（fixture: %s，%d 个文件）"
              % (mark, r["task"], r["fixture"], len(r["fixture_files"])))
        for p in r["problems"]:
            bad += 1
            print("      - %s" % p)
    return 1 if bad else 0


def cmd_run(args):
    tasks = bench.load_tasks()
    only = _ids(args.only)
    if only:
        tasks = [t for t in tasks if str(t.get("id")) in only]
    if not tasks:
        print("没有匹配的任务")
        return 1
    ablations = _ids(args.ablate)
    groups = [()] + [(a,) for a in ablations]
    results = []
    for group in groups:
        for task in tasks:
            for i in range(max(1, int(args.repeat or 1))):
                label = "基线" if not group else "消融:" + ",".join(group)
                print("→ %-14s %-18s (%d/%d)" % (task.get("id"), label,
                                                 i + 1, max(1, int(args.repeat or 1))))
                r = bench.run_task(task, model=args.model, ablations=group,
                                   keep=bool(args.keep))
                print("   %s · %s · 轮次 %s · 工具 %s · %dms"
                      % ("✓" if r.get("ok") else "✗", r.get("status"),
                         r.get("turns"), r.get("tool_calls"), r.get("ms") or 0))
                for c in r.get("checks") or []:
                    if not c.get("ok"):
                        print("     - %s: %s%s" % (c.get("name"), c.get("detail"),
                                                   "（跳过）" if c.get("skipped") else ""))
                results.append(r)
    meta = {"model": args.model or "", "repeat": int(args.repeat or 1),
            "ablations": ablations}
    d, summary = bench.save_run(results, meta, root=args.out or None)
    print("\n已保存：%s" % d)
    print("通过 %d / %d（%s）" % (summary["passed"], summary["runnable"],
                                 summary["pass_rate"]))
    return 0 if summary["passed"] == summary["runnable"] else 1


def cmd_report(args):
    path = os.path.join(args.run, "summary.md")
    if not os.path.isfile(path):
        print("找不到 %s" % path)
        return 1
    with open(path, "r", encoding="utf-8") as f:
        print(f.read())
    return 0


def cmd_review(args):
    out = bench.render_review(args.a, args.b, args.out)
    print("已生成盲评页：%s" % out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Galgame Monster 评测跑分")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出任务")
    sub.add_parser("dry-run", help="只校验任务集与 fixture")

    p_run = sub.add_parser("run", help="跑任务（会调用模型）")
    p_run.add_argument("--only", default="", help="任务 id，逗号分隔")
    p_run.add_argument("--model", default="", help="模型名（默认用聊天配置）")
    p_run.add_argument("--repeat", type=int, default=1, help="每个任务重复次数")
    p_run.add_argument("--ablate", default="", help="消融项，逗号分隔：" +
                       ",".join(bench.ABLATIONS))
    p_run.add_argument("--keep", action="store_true", help="保留临时工作区")
    p_run.add_argument("--out", default="", help="结果根目录")

    p_rep = sub.add_parser("report", help="打印某次运行的摘要")
    p_rep.add_argument("--run", required=True, help="运行目录")

    p_rev = sub.add_parser("review", help="生成 A/B 盲评 HTML")
    p_rev.add_argument("--a", required=True, help="运行目录 A")
    p_rev.add_argument("--b", required=True, help="运行目录 B")
    p_rev.add_argument("--out", default="data/bench/review.html", help="输出 HTML")

    args = parser.parse_args(argv)
    handlers = {"list": cmd_list, "dry-run": cmd_dry, "run": cmd_run,
                "report": cmd_report, "review": cmd_review}
    if args.cmd not in handlers:
        parser.print_help()
        return 2
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
