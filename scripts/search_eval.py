# -*- coding: utf-8 -*-
"""离线检索评测入口。

    python scripts/search_eval.py
    python scripts/search_eval.py --json
    python scripts/search_eval.py --golden tests/search_eval/golden.jsonl

默认阈值：domain_hit >= 0.8、citation_ok == 1.0、unsupported_ok >= 0.9。
不联网、不调模型；用于改排序 / 引用校验后的快速回归。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import search_eval  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=search_eval.default_golden_path())
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    ap.add_argument("--min-domain-hit", type=float, default=0.8)
    ap.add_argument("--min-citation-ok", type=float, default=1.0)
    ap.add_argument("--min-unsupported-ok", type=float, default=0.9)
    args = ap.parse_args()

    cases = search_eval.load_cases(args.golden)
    result = search_eval.evaluate(cases)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for r in result["cases"]:
            flag = "✓" if (r["domain_ok"] and r["bad_ok"] and r["unsupported_ok"]) else "✗"
            print("%s %-22s top=%-28s div=%.2f bad=%s unsupported=%d"
                  % (flag, r["id"], r["top_host"] or "-", r["diversity"],
                     r["bad"], r["unsupported"]))
        m = result["metrics"]
        print("\nmetrics: cases=%d domain_hit=%.3f citation_ok=%.3f "
              "unsupported_ok=%.3f avg_diversity=%.2f"
              % (m["cases"], m["domain_hit"], m["citation_ok"],
                 m["unsupported_ok"], m["avg_diversity"]))

    m = result["metrics"]
    failed = []
    if m["domain_hit"] < args.min_domain_hit:
        failed.append("domain_hit")
    if m["citation_ok"] < args.min_citation_ok:
        failed.append("citation_ok")
    if m["unsupported_ok"] < args.min_unsupported_ok:
        failed.append("unsupported_ok")
    if failed:
        print("FAIL: " + "、".join(failed))
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
