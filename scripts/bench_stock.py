# -*- coding: utf-8 -*-
"""模拟股市性能基准：先测 before，优化后复跑 after，再对比出报告。

用法：
    python scripts/bench_stock.py --tag before --scale 0        # 优化前：只测核心操作
    python scripts/bench_stock.py --tag after  --scale 1000000  # 优化后：核心操作 + 百万级检索/回溯

结果写到 scripts/_bench_<tag>.json，同时在终端打印 avg/p95/p99（毫秒）。
两个 tag 的 json 都存在时，最后自动打印 before→after 对比。

设计约定：
- 全部用注入的合成行情，不碰网络，也不碰 data/stock.db（用独立的 _bench_stock.db）。
- 新增的检索/回溯接口用 hasattr 探测：优化前它们还不存在，脚本照样能跑出核心操作基线。
"""
import argparse
import datetime
import json
import os
import random
import shutil
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import stock  # noqa: E402

BENCH_DB = os.path.join(ROOT, "data", "_bench_stock.db")

SYMBOLS = ["sh600000", "sh600519", "sz000001", "sz300750", "sh510300", "sh688981"]
NAMES = {
    "sh600000": "浦发银行", "sh600519": "贵州茅台", "sz000001": "平安银行",
    "sz300750": "宁德时代", "sh510300": "沪深300ETF", "sh688981": "中芯国际",
}

WORDS = ["大盘", "反弹", "突破", "均线", "估值", "风险", "现金流", "龙头", "缩量",
         "放量", "止损", "加仓", "情绪", "政策", "业绩", "分红", "回撤", "趋势",
         "超跌", "均值回归", "仓位", "换手", "板块", "北向"]
KINDS = ["大盘", "个股", "持仓", "风险", "机会", "复盘"]


def _series(base, n, start=datetime.date(2026, 1, 1)):
    out = []
    for i in range(n):
        o = base + i * 0.2
        c = o + (0.5 if i % 2 == 0 else -0.3)
        out.append({
            "date": (start + datetime.timedelta(days=i)).isoformat(),
            "open": o, "close": c, "high": max(o, c) + 0.4, "low": min(o, c) - 0.4,
            "volume": 100000 + i * 1000, "amount": 0,
        })
    return out


def _pct(samples, p):
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _stats(samples):
    if not samples:
        return {"n": 0, "avg": 0, "p95": 0, "p99": 0, "min": 0, "max": 0}
    return {
        "n": len(samples),
        "avg": round(sum(samples) / len(samples), 3),
        "p95": _pct(samples, 95),
        "p99": _pct(samples, 99),
        "min": round(min(samples), 3),
        "max": round(max(samples), 3),
    }


def _measure(name, fn, repeat, cleanup=None):
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        try:
            fn()
        finally:
            t1 = time.perf_counter()
        samples.append((t1 - t0) * 1000.0)
        if cleanup:
            cleanup()
    return _stats(samples)


def _measure_pairs(prep, act, repeat):
    """测「只算 act」的耗时：prep 放在计时之外，act 放在计时之内。"""
    samples = []
    for _ in range(repeat):
        ctx = prep()
        t0 = time.perf_counter()
        act(ctx)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return _stats(samples)


def reset_db():
    for suffix in ("", "-wal", "-shm"):
        p = BENCH_DB + suffix
        if os.path.exists(p):
            os.remove(p)
    stock.configure(BENCH_DB)
    stock.reset_store()


def _new_game(days=60, ai_count=3):
    bars = {s: _series(10.0 + i, days + 160) for i, s in enumerate(SYMBOLS)}
    bench = _series(4000.0, days + 160)
    return stock.create_game(
        mode="replay", symbols=list(SYMBOLS), days=days, ai_count=ai_count,
        initial_cash=1000000, names=NAMES, bars_by_symbol=bars, benchmark_bars=bench)


# ------------------------------------------------------------
# A. 核心操作
# ------------------------------------------------------------

def bench_core(conn, repeat):
    out = {}
    game = _new_game(days=60)
    gid = game["id"]
    human = "you"
    sym = SYMBOLS[0]

    out["state"] = _measure("state", lambda: stock.game_state(gid), repeat)
    holder = {}
    out["order_place"] = _measure(
        "order_place",
        lambda: holder.__setitem__("id", stock.place_order(gid, human, sym, "buy", 100)["id"]),
        repeat,
        cleanup=lambda: holder.get("id") and stock.cancel_order(gid, holder["id"], human))
    out["order_cancel"] = _measure_pairs(
        lambda: stock.place_order(gid, human, sym, "buy", 100),
        lambda o: stock.cancel_order(gid, o["id"], human), repeat)
    out["bars"] = _measure("bars", lambda: stock.bars_for(gid, sym, 120), repeat)
    out["list_games"] = _measure("list_games", lambda: stock.list_games(), repeat)

    # 推进交易日：单开一局，一路推进到结束
    adv = _new_game(days=60)
    adv_id = adv["id"]
    samples = []
    for _ in range(200):
        st = stock.game_state(adv_id)
        if st["status"] == "finished":
            break
        t0 = time.perf_counter()
        stock.next_day(adv_id)
        samples.append((time.perf_counter() - t0) * 1000.0)
    out["next_day"] = {
        "n": len(samples),
        "avg": round(sum(samples) / max(1, len(samples)), 3),
        "p95": _pct(samples, 95), "p99": _pct(samples, 99),
        "min": round(min(samples) or 0, 3), "max": round(max(samples) or 0, 3),
    }

    # 载荷大小：整份 state vs 增量 patch（patch 是优化后才有的）
    full = json.dumps(stock.game_state(gid), ensure_ascii=False)
    out["payload"] = {"full_state_bytes": len(full.encode("utf-8"))}
    for cand in ("game_patch", "state_patch", "delta_state", "change_patch"):
        fn = getattr(stock, cand, None)
        if callable(fn):
            try:
                patch = fn(gid)
            except TypeError:
                patch = fn(gid, "all")
            pj = json.dumps(patch, ensure_ascii=False)
            out["payload"]["patch_bytes"] = len(pj.encode("utf-8"))
            out["payload"]["patch_fn"] = cand
            break
    return out


# ------------------------------------------------------------
# B. 新功能（优化后才存在，按 hasattr 探测）
# ------------------------------------------------------------

def bench_new(conn, repeat):
    out = {}
    game = _new_game(days=60)
    gid = game["id"]
    dates = _dates_of(conn, gid)
    mid = dates[len(dates) // 2] if dates else ""
    if len(dates) < 3:
        dates = dates + [dates[-1]] * (3 - len(dates))
    elif len(dates) > 3:
        dates = [dates[0], mid, dates[-1]]

    if hasattr(stock, "timeline_dates"):
        out["timeline_dates"] = _measure("timeline_dates",
                                         lambda: stock.timeline_dates(gid), repeat)
    if hasattr(stock, "state_at"):
        out["state_at_mid"] = _measure("state_at_mid",
                                       lambda: stock.state_at(gid, mid), repeat)
    if hasattr(stock, "compare_states"):
        out["compare_states"] = _measure(
            "compare_states",
            lambda: stock.compare_states(gid, [dates[0], mid, dates[-1]]), repeat)
    if hasattr(stock, "trades_for"):
        out["trades_for"] = _measure("trades_for",
                                     lambda: stock.trades_for(gid, SYMBOLS[0]), repeat)
    return out


def _dates_of(conn, gid):
    rows = conn.execute(
        "SELECT DISTINCT date FROM bars WHERE game_id=? AND symbol=? ORDER BY date",
        (gid, stock.BENCHMARK_SYMBOL)).fetchall()
    return [r["date"] for r in rows]


# ------------------------------------------------------------
# C. 大规模检索（百万级）
# ------------------------------------------------------------

def seed_scale(conn, analyses_n, bars_days=800, trades_n=12000):
    """直接写库灌入百万级分析记录 + 一个高交易量的对局，用于压检索/回溯。"""
    gid = "st_bench_big"
    start = datetime.date(2016, 1, 1)
    end = start + datetime.timedelta(days=bars_days - 1)
    conn.execute(
        "INSERT OR REPLACE INTO games(id,name,mode,status,created_at,start_date,end_date,"
        "cursor_date,initial_cash,commission,min_commission,stamp_duty,transfer_fee,"
        "slippage,benchmark) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (gid, "压测局", "replay", "running", time.time(), start.isoformat(), end.isoformat(),
         end.isoformat(), 1000000.0, stock.DEFAULT_FEES["commission"],
         stock.DEFAULT_FEES["min_commission"], stock.DEFAULT_FEES["stamp_duty"],
         stock.DEFAULT_FEES["transfer_fee"], stock.DEFAULT_FEES["slippage"],
         stock.BENCHMARK_SYMBOL))
    for pid, kind in (("you", "human"), ("ai_value", "ai"), ("ai_trend", "ai"),
                      ("ai_contrarian", "ai")):
        conn.execute(
            "INSERT OR REPLACE INTO players(id,game_id,name,kind,persona,cash,initial_cash,"
            "realized_pnl) VALUES(?,?,?,?,?,?,?,0)",
            (pid, gid, pid, kind, "", 1000000.0, 1000000.0))
    for s in list(SYMBOLS) + [stock.BENCHMARK_SYMBOL]:
        conn.execute(
            "INSERT OR REPLACE INTO symbols(game_id,symbol,name,kind,tradable) VALUES(?,?,?,?,?)",
            (gid, s, NAMES.get(s, stock.BENCHMARK_NAME), "stock", 1))
        base = 4000.0 if s == stock.BENCHMARK_SYMBOL else 10.0
        bars = _series(base, bars_days, start)
        conn.executemany(
            "INSERT OR REPLACE INTO bars(game_id,symbol,date,open,high,low,close,volume,amount) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(gid, s, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"], 0)
             for b in bars])
    # 成交记录：百万级库里单个对局最多 ~1.2 万笔，用来压回溯重放
    random.seed(7)
    trade_dates = [(start + datetime.timedelta(days=i)).isoformat() for i in range(bars_days)]
    rows = []
    for i in range(trades_n):
        d = trade_dates[random.randint(0, bars_days - 1)]
        s = SYMBOLS[i % len(SYMBOLS)]
        rows.append(("tr_b%08d" % i, gid, random.choice(["you", "ai_value", "ai_trend"]),
                     s, random.choice(["buy", "buy", "sell"]), d, 100, 10.0 + i % 7,
                     5.0, 1000.0 + i, 0.0, time.time()))
    conn.executemany(
        "INSERT OR REPLACE INTO trades(id,game_id,player_id,symbol,side,date,qty,price,fee,"
        "amount,realized_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    # 分析记录：kind / date / keywords 随机，text 用中文词池拼，供关键词检索
    batch = []
    batch_sym = []
    total = max(0, int(analyses_n))
    for i in range(total):
        date = trade_dates[i % bars_days]
        kind = KINDS[i % len(KINDS)]
        w1 = WORDS[i % len(WORDS)]
        w2 = WORDS[(i * 7 + 3) % len(WORDS)]
        text = "%s：%s 后面看 %s 的延续性，仓位控制放在第一位。" % (w1, NAMES[SYMBOLS[i % len(SYMBOLS)]], w2)
        aid = "an_b%08d" % i
        batch.append((aid, gid, random.choice(["ai_value", "ai_trend", "ai_contrarian"]),
                      date, kind, text, w1 + "," + w2, time.time()))
        batch_sym.append((gid, aid, SYMBOLS[i % len(SYMBOLS)]))
        if len(batch) >= 25000:
            conn.executemany(
                "INSERT OR REPLACE INTO analyses(id,game_id,player_id,date,kind,text,"
                "keywords,created_at) VALUES(?,?,?,?,?,?,?,?)", batch)
            conn.executemany(
                "INSERT OR REPLACE INTO analysis_symbols(game_id,analysis_id,symbol) "
                "VALUES(?,?,?)", batch_sym)
            batch, batch_sym = [], []
    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO analyses(id,game_id,player_id,date,kind,text,"
            "keywords,created_at) VALUES(?,?,?,?,?,?,?,?)", batch)
        conn.executemany(
            "INSERT OR REPLACE INTO analysis_symbols(game_id,analysis_id,symbol) VALUES(?,?,?)",
            batch_sym)
    conn.commit()
    stock.rebuild_analyses_fts(conn)      # 批量直插绕过了索引同步，这里整体重建
    return gid


def bench_scale(conn, analyses_n, repeat):
    out = {}
    t0 = time.perf_counter()
    gid = seed_scale(conn, analyses_n)
    out["seed_seconds"] = round(time.perf_counter() - t0, 1)
    scale = {}
    if hasattr(stock, "search_analyses"):
        date_from = "2016-01-01"
        date_to = "2030-01-01"
        scale["search_page1"] = _measure(
            "search_page1",
            lambda: stock.search_analyses(gid, q="突破", kind="", symbol="",
                                          date_from=date_from, date_to=date_to,
                                          page=1, size=20), repeat)
        scale["search_page_last"] = _measure(
            "search_page_last",
            lambda: stock.search_analyses(gid, q="突破", kind="个股", symbol=SYMBOLS[1],
                                          date_from=date_from, date_to=date_to,
                                          page=500, size=20), repeat)
        scale["search_symbol_only"] = _measure(
            "search_symbol_only",
            lambda: stock.search_analyses(gid, q="", kind="", symbol=SYMBOLS[2],
                                          date_from=date_from, date_to=date_to,
                                          page=1, size=20), repeat)
    if hasattr(stock, "state_at"):
        last = "2018-03-31"
        scale["state_at_late"] = _measure("state_at_late",
                                          lambda: stock.state_at(gid, last), repeat)
    out["scale"] = scale
    return out


# ------------------------------------------------------------
# 报告
# ------------------------------------------------------------

def write_report(tag, report):
    path = os.path.join(ROOT, "scripts", "_bench_%s.json" % tag)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n写入 " + os.path.relpath(path, ROOT))

    print("\n== 延迟（毫秒，n=%s）==" % report.get("repeat"))
    print("%-22s %8s %8s %8s %8s" % ("操作", "avg", "p95", "p99", "max"))
    for k, v in report["core"].items():
        if isinstance(v, dict) and "avg" in v:
            print("%-22s %8.3f %8.3f %8.3f %8.3f" % (k, v["avg"], v["p95"], v["p99"], v["max"]))
    for k, v in (report.get("new") or {}).items():
        if isinstance(v, dict) and "avg" in v:
            print("%-22s %8.3f %8.3f %8.3f %8.3f" % (k, v["avg"], v["p95"], v["p99"], v["max"]))
    payload = report["core"].get("payload")
    if payload:
        print("\n载荷：%s" % payload)
    if report.get("scale"):
        print("\n大规模（%s 条分析）：seed 用时 %ss" % (
            report["analyses_n"], report["scale"].get("seed_seconds")))
        for k, v in (report["scale"].get("scale") or {}).items():
            if "avg" in v:
                print("%-22s %8.3f %8.3f %8.3f %8.3f" % (k, v["avg"], v["p95"], v["p99"], v["max"]))


def compare(tag):
    if tag not in ("before", "after"):
        return                                  # 临时 tag（排查用）不参与对比
    p_before = os.path.join(ROOT, "scripts", "_bench_before.json")
    p_after = os.path.join(ROOT, "scripts", "_bench_after.json")
    if not (os.path.exists(p_before) and os.path.exists(p_after)):
        return
    print("\n== before → after 对比 ==")
    b = json.load(open(p_before, encoding="utf-8"))
    a = json.load(open(p_after, encoding="utf-8"))
    print("载荷：before %s / after %s" % (b["core"].get("payload"), a["core"].get("payload")))
    print("%-22s %10s %10s %10s" % ("操作(avg ms)", "before", "after", "变化"))
    for k in sorted(set(b.get("core", {})) | set(a.get("core", {}))):
        bv = b.get("core", {}).get(k)
        av = a.get("core", {}).get(k)
        if not (isinstance(bv, dict) and isinstance(av, dict) and "avg" in bv and "avg" in av):
            continue
        ratio = (av["avg"] / bv["avg"] * 100.0) if bv["avg"] else 0.0
        print("%-22s %10.3f %10.3f %9.1f%%" % (k, bv["avg"], av["avg"], ratio))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="before")
    ap.add_argument("--scale", type=int, default=0, help="灌入的分析记录条数，0 = 不灌")
    ap.add_argument("--repeat", type=int, default=30)
    ap.add_argument("--keep", action="store_true", help="保留压测库（默认删除）")
    args = ap.parse_args()

    reset_db()
    conn = stock.get_db()
    t0 = time.perf_counter()
    report = {"tag": args.tag, "repeat": args.repeat, "analyses_n": args.scale,
              "core": bench_core(conn, args.repeat)}
    report["new"] = bench_new(conn, min(5, args.repeat))
    if args.scale > 0:
        report["scale"] = bench_scale(conn, args.scale, min(10, args.repeat))
    report["seconds"] = round(time.perf_counter() - t0, 1)
    write_report(args.tag, report)

    if not args.keep:
        try:
            conn.close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            p = BENCH_DB + suffix
            if os.path.exists(p):
                os.remove(p)
    compare(args.tag)


if __name__ == "__main__":
    main()
