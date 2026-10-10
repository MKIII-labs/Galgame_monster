# -*- coding: utf-8 -*-
"""Token 用量：JSONL 流水与按天/月聚合"""
import json
import os
import time

from app.store import USAGE_FILE, _lock

# ============================================================
# Token 用量统计
# ============================================================

def record_usage(entry):
    """追加一条用量记录（JSONL，O(1) 追加）"""
    try:
        with _lock:
            with open(USAGE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[用量] 写入失败：{e}")

def load_usage():
    rows = []
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            print(f"[用量] 读取失败：{e}")
    return rows

def _bucket_key(ts, mode):
    t = time.localtime(ts)
    return time.strftime("%Y-%m", t) if mode == "month" else time.strftime("%Y-%m-%d", t)

def _bucket_keys(mode, span):
    """生成从今往前的连续时间桶（补全无数据的日期，折线图才连续）"""
    t = time.localtime(time.time())
    keys = []
    if mode == "month":
        y, m = t.tm_year, t.tm_mon
        for i in range(span - 1, -1, -1):
            mm, yy = m - i, y
            while mm <= 0:
                mm += 12
                yy -= 1
            keys.append("%04d-%02d" % (yy, mm))
    else:
        base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        for i in range(span - 1, -1, -1):
            keys.append(time.strftime("%Y-%m-%d", time.localtime(base - i * 86400)))
    return keys

def aggregate_usage(mode="day", span=30):
    """按 天/月 聚合用量，供折线图与明细表使用"""
    keys = _bucket_keys(mode, span)
    key_set = set(keys)
    models, series = [], {}
    table, by_model, by_source = {}, {}, {}
    summary = {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "calls": 0}

    for r in load_usage():
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        key = _bucket_key(ts, mode)
        if key not in key_set:
            continue
        model = r.get("model") or "未知模型"
        source = r.get("source") or "chat"
        p = int(r.get("prompt") or 0)
        c = int(r.get("completion") or 0)
        tot = int(r.get("total") or (p + c))
        cached = int(r.get("cached") or 0)

        if model not in series:
            series[model] = {k: 0 for k in keys}
            models.append(model)
        series[model][key] += tot

        def bump(d, k, extra=None):
            it = d.setdefault(k, {"prompt": 0, "completion": 0, "total": 0,
                                  "cached": 0, "calls": 0})
            it["prompt"] += p
            it["completion"] += c
            it["total"] += tot
            it["cached"] += cached
            it["calls"] += 1

        bump(table, (key, model))
        bump(by_model, model)
        bump(by_source, source)
        summary["prompt"] += p
        summary["completion"] += c
        summary["total"] += tot
        summary["cached"] += cached
        summary["calls"] += 1

    rows = [{"bucket": k, "model": m, **v} for (k, m), v in table.items()]
    rows.sort(key=lambda x: (x["bucket"], x["model"]), reverse=True)
    model_rows = [{"model": m, **v} for m, v in by_model.items()]
    model_rows.sort(key=lambda x: x["total"], reverse=True)

    return {
        "mode": mode,
        "span": span,
        "labels": keys,
        "models": models,
        "series": series,
        "summary": summary,
        "rows": rows,
        "by_model": model_rows,
        "by_source": by_source,
    }
