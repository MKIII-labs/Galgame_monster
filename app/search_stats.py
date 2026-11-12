# -*- coding: utf-8 -*-
"""联网搜索运行指标（P2）：JSONL 追加 + 汇总 P50/P95。

只记录次数 / 延迟 / 命中 / 抓取条数，不记录 query 正文，避免把用户问题落盘。
用于 scripts/search_stats_report.py 的看板式汇总。
"""
import json
import os
import threading
import time

from app.store import DATA_DIR

FILE = os.path.join(DATA_DIR, "cache", "search_stats.jsonl")
MAX_BYTES = 2 * 1024 * 1024
_LOCK = threading.Lock()


def record(entry):
    if not isinstance(entry, dict):
        return
    e = dict(entry)
    e.setdefault("ts", time.time())
    line = json.dumps(e, ensure_ascii=False)
    with _LOCK:
        try:
            os.makedirs(os.path.dirname(FILE), exist_ok=True)
            with open(FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if os.path.getsize(FILE) > MAX_BYTES:
                _prune()
        except Exception:                    # noqa: BLE001  指标失败不能影响搜索
            pass


def _prune():
    """文件过大时只留最近 3000 条。"""
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        keep = lines[-3000:]
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, FILE)
    except Exception:                        # noqa: BLE001
        pass


def load():
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            rows = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
            return rows
    except Exception:                        # noqa: BLE001
        return []


def _pct(values, p):
    if not values:
        return 0
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(round((p / 100.0) * (len(vals) - 1)))))
    return vals[idx]


def summary(entries=None):
    rows = entries if entries is not None else load()
    if not rows:
        return {"count": 0, "p50_ms": 0, "p95_ms": 0, "avg_hits": 0,
                "avg_fetched": 0, "research_ratio": 0, "empty_rate": 0}
    ms = []
    hits = []
    fetched = []
    research = 0
    empty = 0
    for r in rows:
        try:
            ms.append(int(float(r.get("ms") or 0)))
        except (TypeError, ValueError):
            pass
        try:
            h = int(float(r.get("hits") or 0))
        except (TypeError, ValueError):
            h = 0
        hits.append(h)
        if h <= 0:
            empty += 1
        try:
            fetched.append(int(float(r.get("fetched") or 0)))
        except (TypeError, ValueError):
            fetched.append(0)
        if r.get("research"):
            research += 1
    n = len(rows)
    return {
        "count": n,
        "p50_ms": _pct(ms, 50),
        "p95_ms": _pct(ms, 95),
        "avg_hits": round(sum(hits) / n, 2),
        "avg_fetched": round(sum(fetched) / n, 2),
        "research_ratio": round(research / n, 3),
        "empty_rate": round(empty / n, 3),
    }
