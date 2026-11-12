# -*- coding: utf-8 -*-
"""联网搜索指标看板（命令行）。

    python scripts/search_stats_report.py
    python scripts/search_stats_report.py --json

读取 data/cache/search_stats.jsonl，输出次数 / P50 / P95 / 平均命中 / 平均抓取 / 空结果率。
不记录 query 正文，只做运行状况汇总。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import search_stats  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    s = search_stats.summary()
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    if not s["count"]:
        print("还没有搜索指标（data/cache/search_stats.jsonl 为空）。")
        return 0
    print("搜索次数：%d" % s["count"])
    print("延迟：P50 %d ms / P95 %d ms" % (s["p50_ms"], s["p95_ms"]))
    print("平均命中：%.2f 条 · 平均抓取：%.2f 页" % (s["avg_hits"], s["avg_fetched"]))
    print("深度检索占比：%.1f%% · 空结果率：%.1f%%"
          % (s["research_ratio"] * 100, s["empty_rate"] * 100))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
