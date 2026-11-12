# -*- coding: utf-8 -*-
"""离线检索评测（P2）：金标 query + 候选结果 + 示例回答，算排序 / 引用指标。

不联网、不调模型：
- 排序指标：top1 是否命中期望域名、top3 域名多样性
- 引用指标：编号是否都存在、数字 / 标识符是否能在被引来源的逐字证据里找到

金标文件在 tests/search_eval/golden.jsonl；scripts/search_eval.py 是命令行入口。
"""
import json
import os
import tempfile
from urllib.parse import urlparse

from app import research, search_rank


def _host(url):
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def load_cases(path):
    cases = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cases.append(json.loads(line))
    return cases


def evaluate(cases, data_dir=None):
    """返回 {"cases": [...], "metrics": {...}}；data_dir 为空时用临时目录。"""
    old_dir = research.DATA_DIR
    tmp = data_dir or tempfile.mkdtemp(prefix="gm_search_eval_")
    research.DATA_DIR = tmp
    rows = []
    try:
        for case in cases:
            query = str(case.get("query") or "")
            intent = str(case.get("intent") or "general")
            freshness = str(case.get("freshness") or "")
            ranked = search_rank.rank_hits(
                case.get("results") or [], [query], intent=intent,
                freshness=freshness, semantic=False)
            top_host = _host((ranked[0] or {}).get("url")) if ranked else ""
            expect_domains = [str(x).lower() for x in (case.get("expect_domains") or [])]
            domain_ok = bool(top_host) and any(d in top_host for d in expect_domains)
            hosts = [_host(it.get("url")) for it in ranked[:3]]
            hosts = [h for h in hosts if h]
            diversity = (len(set(hosts)) / min(3, len(hosts))) if hosts else 0.0

            led = research.Ledger("eval_" + str(case.get("id") or len(rows)))
            for it in ranked[:6]:
                d = dict(it)
                if not d.get("evidence") and d.get("content"):
                    d["evidence"] = [{"quote": str(d["content"])[:600],
                                      "line_start": 1, "line_end": 1}]
                led.add_sources([d], query=query)
            audit = led.audit_citations(str(case.get("answer") or ""))
            expect_bad = sorted(int(x) for x in (case.get("expect_bad") or []))
            bad_ok = sorted(audit.get("bad") or []) == expect_bad
            unsupported_ok = bool(audit.get("unsupported")) == bool(case.get("expect_unsupported"))
            rows.append({
                "id": case.get("id") or "",
                "top_host": top_host,
                "domain_ok": domain_ok,
                "diversity": round(diversity, 3),
                "bad": audit.get("bad") or [],
                "unsupported": len(audit.get("unsupported") or []),
                "bad_ok": bad_ok,
                "unsupported_ok": unsupported_ok,
            })
    finally:
        research.DATA_DIR = old_dir
    n = max(1, len(rows))
    metrics = {
        "cases": len(rows),
        "domain_hit": round(sum(1 for r in rows if r["domain_ok"]) / n, 4),
        "citation_ok": round(sum(1 for r in rows if r["bad_ok"]) / n, 4),
        "unsupported_ok": round(sum(1 for r in rows if r["unsupported_ok"]) / n, 4),
        "avg_diversity": round(sum(float(r["diversity"]) for r in rows) / n, 4),
    }
    return {"cases": rows, "metrics": metrics}


def default_golden_path():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "tests", "search_eval", "golden.jsonl")
