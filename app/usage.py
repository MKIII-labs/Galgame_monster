# -*- coding: utf-8 -*-
"""Token 用量：JSONL 流水与按天/月聚合"""
import json
import os
import time
from urllib.parse import urlparse

from app.store import USAGE_FILE, _lock

# ============================================================
# Token 用量统计
# ============================================================

USAGE_MAX_BYTES = 8 * 1024 * 1024      # 流水文件超过 8MB 就裁剪
USAGE_KEEP_LINES = 20000               # 裁剪后保留最近多少条

UNKNOWN_ENDPOINT = "未记录端点"          # 旧流水没有端点字段时的占位


def endpoint_of(url):
    """从接口地址提取服务商端点（域名小写），用于按账户 / 服务商溯源。"""
    try:
        host = (urlparse((url or "").strip()).netloc or "").lower()
    except Exception:
        host = ""
    return host or UNKNOWN_ENDPOINT


def usage_counts(u):
    """把各家响应的 usage 统一成 {prompt, completion, total, cached}。

    OpenAI 兼容用 prompt_tokens / completion_tokens；DashScope 原生用
    input_tokens / output_tokens。缺 usage 时返回 None。
    """
    if not isinstance(u, dict) or not u:
        return None

    def _i(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    p = _i(u.get("prompt_tokens") or u.get("input_tokens"))
    c = _i(u.get("completion_tokens") or u.get("output_tokens"))
    d = u.get("prompt_tokens_details") or {}
    return {"prompt": p, "completion": c,
            "total": _i(u.get("total_tokens")) or (p + c),
            "cached": _i(u.get("prompt_cache_hit_tokens")) or _i(d.get("cached_tokens"))}


def record_llm_usage(u, model, source, api="chat", url=""):
    """把一次模型响应的 usage 写入流水（缺失 usage 时静默跳过）。

    api 记录接入通道（chat / light / vision），endpoint 记录服务商域名，
    两者共同支撑「账户总量」与「按服务商溯源」。
    """
    counts = usage_counts(u)
    if not counts:
        return
    record_usage({"ts": time.time(), "source": source, "api": api,
                  "endpoint": endpoint_of(url), "model": model, **counts})


def record_usage(entry):
    """追加一条用量记录（JSONL，O(1) 追加）"""
    try:
        with _lock:
            with open(USAGE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _rotate_if_needed()
    except Exception as e:
        print(f"[用量] 写入失败：{e}")


def _rotate_if_needed():
    """调用方需已持有 _lock。流水只增不减，涨太大就截断成最近若干条 ——
    本地小工具，用量明细不需要永久归档。"""
    try:
        if os.path.getsize(USAGE_FILE) <= USAGE_MAX_BYTES:
            return
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        keep = lines[-USAGE_KEEP_LINES:]
        tmp = USAGE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, USAGE_FILE)
        print(f"[用量] 流水过大，已裁剪为最近 {len(keep)} 条")
    except OSError as e:
        print(f"[用量] 裁剪失败：{e}")

_TODAY_CACHE = {"at": 0.0, "total": 0}


def today_total(ttl=60):
    """今天已经用掉的 token（带缓存，避免每轮请求都扫全量流水）。"""
    now = time.time()
    if now - _TODAY_CACHE["at"] < ttl:
        return _TODAY_CACHE["total"]
    today = time.strftime("%Y-%m-%d")
    total = 0
    for r in load_usage():
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if _bucket_key(ts, "day") != today:
            continue
        total += int(r.get("total") or 0)
    _TODAY_CACHE.update(at=now, total=total)
    return total


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


def rounds_for_turn(done_at, rounds=0, window=8.0, tail_bytes=1_000_000,
                    session_id=""):
    """从用量流水尾部找「结束时间匹配」的回合，返回 timing.rounds_detail。

    旧消息的 timing 里没有逐轮明细，只能靠 done_at 与回合结束时的 usage 记录
    对齐；只读尾部 1MB，避免每次打开明细都解析整个 8MB 流水。
    新记录带 session_id，能进一步排除同一时刻别的会话。
    """
    try:
        done = float(done_at or 0)
    except (TypeError, ValueError):
        done = 0
    if done <= 0:
        return []
    try:
        size = os.path.getsize(USAGE_FILE)
        with open(USAGE_FILE, "rb") as f:
            f.seek(max(0, size - max(4096, int(tail_bytes))))
            raw = f.read().decode("utf-8", "ignore")
    except OSError:
        return []
    lines = raw.splitlines()
    if len(lines) > 1 and not lines[0].lstrip().startswith("{"):
        lines = lines[1:]          # 从中间截断的第一行丢掉
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if str(e.get("source") or "") != "chat":
            continue
        if session_id and e.get("session_id") and str(e["session_id"]) != str(session_id):
            continue
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if abs(ts - done) > window:
            continue
        t = e.get("timing") or {}
        if rounds:
            try:
                if int(t.get("rounds") or 0) != int(rounds):
                    continue
            except (TypeError, ValueError):
                pass
        rd = t.get("rounds_detail") or []
        if rd:
            return rd
    return []

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

def aggregate_usage(mode="day", span=30, call_limit=200):
    """按 天/月 聚合用量。

    返回分三层：
    - 明细层：rows（按桶 × 模型）、calls（逐次调用，用于溯源）
    - 汇总层：by_model / by_source / by_endpoint / by_api
    - 趋势层：series（按模型的折线）、daily（不分模型的堆叠柱）
    外加 summary 与 derived（命中率、平均单次、活跃桶、峰值等派生指标）。
    """
    keys = _bucket_keys(mode, span)
    key_set = set(keys)
    models, series = [], {}
    table, by_model, by_source, by_endpoint, by_api, calls = {}, {}, {}, {}, {}, []
    # 不分模型的按桶汇总：柱状图用（堆叠输入/输出 + 调用次数）
    daily = {k: {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "calls": 0}
             for k in keys}
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
        # 旧流水没有 api / endpoint：当时只有主对话与轻量调用记账，按 chat 兼容
        api = (r.get("api") or "chat").strip() or "chat"
        endpoint = (r.get("endpoint") or "").strip() or UNKNOWN_ENDPOINT
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
        bump(by_endpoint, endpoint)
        bump(by_api, api)
        day = daily[key]
        day["prompt"] += p
        day["completion"] += c
        day["total"] += tot
        day["cached"] += cached
        day["calls"] += 1
        calls.append({"ts": ts, "api": api, "endpoint": endpoint, "model": model,
                      "source": source, "prompt": p, "completion": c,
                      "total": tot, "cached": cached})
        summary["prompt"] += p
        summary["completion"] += c
        summary["total"] += tot
        summary["cached"] += cached
        summary["calls"] += 1

    rows = [{"bucket": k, "model": m, **v} for (k, m), v in table.items()]
    rows.sort(key=lambda x: (x["bucket"], x["model"]), reverse=True)
    model_rows = [{"model": m, **v} for m, v in by_model.items()]
    model_rows.sort(key=lambda x: x["total"], reverse=True)
    endpoint_rows = [{"endpoint": k, **v} for k, v in by_endpoint.items()]
    endpoint_rows.sort(key=lambda x: x["total"], reverse=True)
    api_rows = [{"api": k, **v} for k, v in by_api.items()]
    api_rows.sort(key=lambda x: x["total"], reverse=True)
    daily_rows = [{"bucket": k, **daily[k]} for k in keys]
    calls.sort(key=lambda x: x["ts"], reverse=True)

    return {
        "mode": mode,
        "span": span,
        "labels": keys,
        "models": models,
        "series": series,
        "daily": daily_rows,
        "summary": summary,
        "derived": _derived(summary, daily_rows),
        "rows": rows,
        "by_model": model_rows,
        "by_source": by_source,
        "by_endpoint": endpoint_rows,
        "by_api": api_rows,
        "calls": calls[:call_limit],
        "calls_total": len(calls),
    }


def _derived(summary, daily_rows):
    """概览卡片的派生指标。分母为 0 时给 None，前端显示「—」而不是 NaN。"""
    prompt = int(summary.get("prompt") or 0)
    total = int(summary.get("total") or 0)
    calls = int(summary.get("calls") or 0)
    active = [d for d in daily_rows if d.get("calls")]
    peak = max(active, key=lambda d: d.get("total") or 0) if active else None
    pct = lambda a, b: round(a * 100.0 / b, 1) if b else None
    return {
        # 缓存命中率的分母是输入：命中本身就是输入的一部分
        "hit_rate": pct(int(summary.get("cached") or 0), prompt),
        "avg_call": int(round(total / calls)) if calls else 0,
        # 只在有消耗的桶上取平均，否则空白期会把日均拉得很低
        "avg_active": int(round(total / len(active))) if active else 0,
        "active": len(active),
        "peak": ({"bucket": peak["bucket"], "total": int(peak.get("total") or 0)}
                 if peak else None),
    }
