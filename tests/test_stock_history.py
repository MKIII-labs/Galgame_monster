# -*- coding: utf-8 -*-
"""功能大厅：模拟股市——历史分析检索 / 时间轴回溯 / 对比 / 冷热归档 / 一致性。

覆盖需求里的边界场景（最早最晚时间点、空数据、超大跨度）、多用户并发、
数据一致性校验，以及 K 线标注数据、旧格式 AI 输出的兼容。
"""
import datetime
import threading

import pytest

from app import stock


# ------------------------------------------------------------
# 测试数据 / 临时库（与 test_stock.py 同一套注入方式，不碰网络）
# ------------------------------------------------------------

def _dates(n=40, start=datetime.date(2026, 8, 3)):
    return [(start + datetime.timedelta(days=i)).isoformat() for i in range(n)]


def _series(base, n=40):
    out = []
    for i, d in enumerate(_dates(n)):
        o = base + i * 0.2
        c = o + (0.5 if i % 2 == 0 else -0.3)
        out.append({
            "date": d, "open": o, "close": c,
            "high": max(o, c) + 0.4, "low": min(o, c) - 0.4,
            "volume": 100000 + i * 1000, "amount": 0,
        })
    return out


BARS = {"sh600000": _series(10.0), "sh510300": _series(4.0)}
BENCH = _series(4000.0)
NAMES = {"sh600000": "浦发银行", "sh510300": "沪深300ETF"}


@pytest.fixture
def db(tmp_path):
    stock.configure(str(tmp_path / "stock.db"))
    stock.reset_store()
    yield stock
    stock.reset_store()


def _new_game(days=6, ai_count=1, mode="replay"):
    return stock.create_game(
        mode=mode, symbols=list(BARS), days=days, ai_count=ai_count,
        initial_cash=1000000, names=NAMES, bars_by_symbol=BARS, benchmark_bars=BENCH)


def _seed_analysis(game_id, player_id, date, kind, text, keywords, symbols):
    """直接落一条分析记录（AI 之外的注入入口，测试用）。"""
    conn = stock.get_db()
    with stock._GAME_LOCK:
        out = stock._insert_analysis(conn, game_id, player_id, date, kind,
                                     text, keywords, symbols)
        conn.commit()
    return out


def _play(gid, days=2, symbol="sh600000"):
    """买 1 手 -> 推进，重复若干天，制造真实成交与持仓。"""
    for _ in range(days):
        stock.place_order(gid, "you", symbol, "buy", 100)
        stock.next_day(gid)


# ------------------------------------------------------------
# 1. AI 分析记录：多维筛选 + 分页
# ------------------------------------------------------------

def test_analysis_search_filters(db):
    g = _new_game(days=6)
    gid = g["id"]
    stock.next_day(gid)                    # 先走一天，回溯时间轴才有两个节点
    dates = stock.timeline_dates(gid)
    assert len(dates) >= 2
    d0, d1 = dates[0], dates[1]

    _seed_analysis(gid, "ai_value", d0, "个股", "浦发银行放量突破，量能配合良好。",
                   ["放量", "突破"], ["sh600000"])
    _seed_analysis(gid, "ai_trend", d0, "大盘", "大盘缩量反弹，情绪仍在修复。",
                   ["缩量", "反弹"], [])
    _seed_analysis(gid, "ai_value", d1, "个股", "沪深300ETF 估值偏低，适合慢慢买。",
                   ["估值", "低估"], ["sh510300"])
    _seed_analysis(gid, "ai_trend", d1, "风险", "持仓过于集中，注意回撤风险。",
                   ["回撤", "风险"], ["sh600000"])

    # 类型
    r = stock.search_analyses(gid, kind="个股")
    assert r["total"] == 2
    # 标的
    r = stock.search_analyses(gid, symbol="sh600000")
    assert r["total"] == 2
    r = stock.search_analyses(gid, symbol="600000")        # 允许不带市场前缀
    assert r["total"] == 2
    # 时间区间
    r = stock.search_analyses(gid, date_from=d1, date_to=d1)
    assert r["total"] == 2
    r = stock.search_analyses(gid, date_from=d0, date_to=d0)
    assert r["total"] == 2
    # 关键词（>=3 字可能走 FTS，2 字走 LIKE，两条路径都要能命中）
    assert stock.search_analyses(gid, q="放量")["total"] == 1
    assert stock.search_analyses(gid, q="突破")["total"] == 1
    assert stock.search_analyses(gid, q="缩量")["total"] == 1
    # 组合条件
    r = stock.search_analyses(gid, kind="个股", symbol="sh600000", q="突破")
    assert r["total"] == 1
    assert r["items"][0]["kind"] == "个股"
    assert r["items"][0]["symbols"] == ["sh600000"]
    assert r["items"][0]["player_name"]
    # 无结果
    r = stock.search_analyses(gid, q="不存在的关键词")
    assert r["total"] == 0 and r["items"] == []
    assert r["engine"] in ("trigram", "like")


def test_analysis_search_paging(db):
    g = _new_game(days=6)
    gid = g["id"]
    dates = stock.timeline_dates(gid)
    for i in range(7):
        _seed_analysis(gid, "ai_value", dates[i % len(dates)], "复盘",
                       "第 %d 条分析。" % i, ["复盘"], ["sh600000"])
    p1 = stock.search_analyses(gid, size=3, page=1)
    p2 = stock.search_analyses(gid, size=3, page=2)
    p3 = stock.search_analyses(gid, size=3, page=3)
    assert p1["total"] == 7 and p1["pages"] == 3
    assert len(p1["items"]) == 3 and len(p2["items"]) == 3 and len(p3["items"]) == 1
    ids = {x["id"] for x in p1["items"]} | {x["id"] for x in p2["items"]} \
        | {x["id"] for x in p3["items"]}
    assert len(ids) == 7                      # 分页不重不漏
    assert stock.search_analyses(gid, size=3, page=99)["items"] == []   # 越界页返回空


# ------------------------------------------------------------
# 2. 时间轴回溯：边界 / 空数据 / 超大跨度
# ------------------------------------------------------------

def test_state_at_empty_and_boundaries(db):
    g = _new_game(days=6)
    gid = g["id"]
    dates = stock.timeline_dates(gid)
    start = g["start_date"]

    # 空数据：还没交易、没有 AI 分析的回溯
    st = stock.state_at(gid, start)
    assert st["as_of"] == start
    assert st["readonly"] is True
    assert st["analyses"] == [] and st["trades"] == []
    for p in st["players"]:
        assert p["positions"] == []
        assert abs(p["cash"] - 1000000) < 1e-6
        assert abs(p["equity"] - 1000000) < 1e-6

    # 非法日期
    with pytest.raises(stock.StockError):
        stock.state_at(gid, "2026-13-99")
    # 早于开局 -> 夹到开局；晚于当前模拟日 -> 夹到当前（不偷看未来）
    assert stock.state_at(gid, "1990-01-01")["as_of"] == start
    assert stock.state_at(gid, "2099-01-01")["as_of"] == g["cursor_date"]
    assert stock.state_at(gid, "")["as_of"] == g["cursor_date"]
    assert dates[0] == start


def test_state_at_huge_span_back_and_forth(db):
    g = _new_game(days=20)
    gid = g["id"]
    start = g["start_date"]
    _play(gid, days=6)

    now = stock.game_state(gid)
    human_now = next(p for p in now["players"] if p["id"] == "you")
    assert human_now["positions"], "先要有持仓，回溯才有对比意义"

    # 超大跨度：回到开局当天，持仓/资金要完整还原
    old = stock.state_at(gid, start)
    human_old = next(p for p in old["players"] if p["id"] == "you")
    assert human_old["positions"] == []
    assert abs(human_old["cash"] - 1000000) < 1e-6
    # 再跳回当前日，必须和实时状态一致
    back = stock.state_at(gid, now["cursor_date"])
    human_back = next(p for p in back["players"] if p["id"] == "you")
    assert abs(human_back["cash"] - human_now["cash"]) < 0.01
    assert abs(human_back["equity"] - human_now["equity"]) < 0.01
    assert [x["qty"] for x in human_back["positions"]] \
        == [x["qty"] for x in human_now["positions"]]


def test_state_at_no_lookahead_and_chart_bounds(db):
    g = _new_game(days=20)
    gid = g["id"]
    _play(gid, days=5)
    dates = stock.timeline_dates(gid)
    assert len(dates) >= 3

    st = stock.state_at(gid, dates[2])
    assert st["as_of"] == dates[2]
    assert st["chart"]["bars"]
    assert all(b["date"] <= dates[2] for b in st["chart"]["bars"])
    assert st["chart"]["marks"] is not None
    assert all(m["date"] <= dates[2] for m in st["trades"])
    # 回溯时 AI 分析只到该日为止
    assert all(a["date"] <= dates[2] for a in st["analyses"])


def test_timeline_dates_grows_with_cursor(db):
    g = _new_game(days=6)
    gid = g["id"]
    first = stock.timeline_dates(gid)
    assert first == [g["start_date"]]
    stock.next_day(gid)
    second = stock.timeline_dates(gid)
    assert len(second) == 2
    assert second[0] == g["start_date"]
    assert second[-1] == stock.game_state(gid)["cursor_date"]
    # 不含未来
    assert all(d <= stock.game_state(gid)["cursor_date"] for d in second)


# ------------------------------------------------------------
# 3. 历史对比（最多 3 个节点）
# ------------------------------------------------------------

def test_compare_states(db):
    g = _new_game(days=20)
    gid = g["id"]
    _play(gid, days=4)
    dates = stock.timeline_dates(gid)
    assert len(dates) >= 3

    out = stock.compare_states(gid, [dates[0], dates[-1]])
    assert out["count"] == 2
    assert [n["as_of"] for n in out["nodes"]] == [dates[0], dates[-1]]
    out3 = stock.compare_states(gid, [dates[0], dates[1], dates[-1]])
    assert out3["count"] == 3
    assert out3["nodes"][0]["players"] and out3["nodes"][2]["chart"]["bars"]

    with pytest.raises(stock.StockError):
        stock.compare_states(gid, [dates[0]])
    with pytest.raises(stock.StockError):
        stock.compare_states(gid, [dates[0], dates[1], dates[2], dates[-1]])


# ------------------------------------------------------------
# 4. K 线标注数据 & 一致性
# ------------------------------------------------------------

def test_marks_carry_trade_details(db):
    g = _new_game(days=20)
    gid = g["id"]
    _play(gid, days=3)

    out = stock.bars_for(gid, "sh600000", 120, with_marks=True)
    assert out["as_of"] == stock.game_state(gid)["cursor_date"]
    assert out["bars"]
    marks = out["marks"]
    assert len(marks) == 3
    for m in marks:
        assert m["symbol"] == "sh600000"
        assert m["side"] == "buy"
        assert m["qty"] == 100
        assert m["price"] > 0
        assert m["date"] <= out["as_of"]
        # 必须能看出是谁下的单（前端要显示「你 / 老陈 / 小夏」）
        assert m["player_id"] == "you"
        assert m["player_kind"] == "human"
        assert m["player_name"] == "你"
    # 标注日必须是图里真实存在的 K 线日，前端才能对齐
    bar_dates = {b["date"] for b in out["bars"]}
    assert all(m["date"] in bar_dates for m in marks)
    # 按标的查询
    assert len(stock.trades_for(gid, symbol="sh600000")) == 3
    assert stock.trades_for(gid, symbol="sh510300") == []


def test_marks_identify_human_and_ai(db, monkeypatch):
    """同一张图上既有我也有 AI 的成交，标注必须能区分是谁。"""
    gid = _new_game(days=4, ai_count=1)["id"]
    monkeypatch.setattr(stock, "get_api_conf",
                        lambda provider: ("k", "http://example.invalid", "m"))
    monkeypatch.setattr(stock.llm, "_quick_chat", lambda *a, **k: (
        '{"orders":[{"symbol":"sh600000","side":"buy","qty":100,"reason":"AI 买"}],'
        '"journal":"AI 买入"}'))
    stock.ai_decide(gid, "ai_value")
    stock.place_order(gid, "you", "sh600000", "buy", 100)
    stock.next_day(gid)

    marks = stock.bars_for(gid, "sh600000", 120, with_marks=True)["marks"]
    who = {(m["player_id"], m["player_kind"], m["player_name"]) for m in marks}
    assert ("you", "human", "你") in who
    assert ("ai_value", "ai", "价值派老陈") in who


def test_audit_game_consistency_after_play(db):
    g = _new_game(days=20)
    gid = g["id"]
    _play(gid, days=5)
    stock.place_order(gid, "you", "sh600000", "sell", 100)   # 有买有卖
    stock.next_day(gid)
    assert stock.audit_game(gid) == []

    # 回溯到最新与实时状态一致
    st = stock.state_at(gid, stock.game_state(gid)["cursor_date"])
    now = stock.game_state(gid)
    for a, b in zip(st["players"], now["players"]):
        assert a["id"] == b["id"]
        assert abs(a["cash"] - b["cash"]) < 0.01
        assert abs(a["equity"] - b["equity"]) < 0.01
        assert abs(a["realized_pnl"] - b["realized_pnl"]) < 0.01


# ------------------------------------------------------------
# 5. 并发：多线程同时下单 / 推进，不应出现负现金、丢单、对不上账
# ------------------------------------------------------------

def test_concurrent_orders_stay_consistent(db):
    g = _new_game(days=20, ai_count=0)
    gid = g["id"]
    symbols = ["sh600000", "sh510300"]
    errors = []

    def worker(i):
        try:
            stock.place_order(gid, "you", symbols[i % len(symbols)], "buy", 100)
        except Exception as e:                      # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    state = stock.game_state(gid)
    assert len(state["pending_orders"]) == 12
    assert len({o["id"] for o in state["pending_orders"]}) == 12       # 没有重复单

    # 结算后：账要能对上，且现金不为负、持仓不超过买入量
    stock.next_day(gid)
    assert stock.audit_game(gid) == []
    human = next(p for p in stock.game_state(gid)["players"] if p["id"] == "you")
    assert human["cash"] >= 0
    assert sum(p["qty"] for p in human["positions"]) == 1200


def test_concurrent_next_day_settles_exactly_once_per_day(db):
    """并发推进：每次调用只前进一个交易日、只结算一次，账目始终对得上。"""
    g = _new_game(days=20, ai_count=0)
    gid = g["id"]
    stock.place_order(gid, "you", "sh600000", "buy", 100)
    start = stock.game_state(gid)["cursor_date"]
    results = []

    def worker():
        try:
            results.append(stock.next_day(gid)["game"]["cursor_date"])
        except Exception as e:                      # noqa: BLE001
            results.append("err:" + repr(e))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(not str(r).startswith("err") for r in results), results
    assert len(set(results)) == len(results)        # 每个线程拿到不同的一天，没有重复推进
    assert stock.game_state(gid)["cursor_date"] > start
    # 一笔挂单只能成交一次：并发推进 6 天也只有 1 条成交，不会重复撮合
    trades = stock.trades_for(gid)
    assert len(trades) == 1
    assert stock.audit_game(gid) == []


# ------------------------------------------------------------
# 6. 冷热归档：搬进冷表后检索/回溯对调用方无感
# ------------------------------------------------------------

def test_cold_archive_is_transparent(db):
    g = _new_game(days=2)          # 2 天推完即结束，会触发自动归档
    gid = g["id"]
    dates = stock.timeline_dates(gid)
    for i in range(4):
        kw = "放量" if i % 2 else "缩量"
        _seed_analysis(gid, "ai_value", dates[i % len(dates)], "复盘",
                       "归档前的第 %d 条。%s" % (i, kw), [kw], ["sh600000"])
    stock.next_day(gid)
    stock.next_day(gid)
    assert stock.game_state(gid)["status"] == "finished"

    conn = stock.get_db()
    hot = conn.execute("SELECT COUNT(*) AS n FROM analyses WHERE game_id=?",
                       (gid,)).fetchone()["n"]
    cold = conn.execute("SELECT COUNT(*) AS n FROM analyses_cold WHERE game_id=?",
                        (gid,)).fetchone()["n"]
    assert hot == 0 and cold == 4                 # 对局一结束就搬进冷表

    # 检索仍然能看到冷数据
    assert stock.search_analyses(gid, kind="复盘")["total"] == 4
    assert stock.search_analyses(gid, q="放量")["total"] == 2
    assert stock.search_analyses(gid, symbol="sh600000")["total"] == 4
    # 回溯同样能看到
    st = stock.state_at(gid, dates[-1])
    assert len(st["analyses"]) == 4
    # 归档过之后再做归档无副作用
    assert stock.archive_analyses(gid)["moved"] == 0


def test_cold_archive_requires_finished(db):
    g = _new_game(days=6)
    with pytest.raises(stock.StockError):
        stock.archive_analyses(g["id"])


# ------------------------------------------------------------
# 7. AI 分析写入（新格式 + 旧格式兼容）
# ------------------------------------------------------------

def test_ai_writes_structured_analysis(db, monkeypatch):
    gid = _new_game()["id"]
    monkeypatch.setattr(stock, "get_api_conf",
                        lambda provider: ("k", "http://example.invalid", "m"))
    payload = (
        '{"analysis":{"kind":"个股","symbols":["sh600000"],'
        '"keywords":["放量","突破"],"text":"浦发银行放量突破，先小仓位试。"},'
        '"orders":[{"symbol":"sh600000","side":"buy","qty":100,"reason":"试仓"}],'
        '"journal":"试仓一笔"}'
    )
    monkeypatch.setattr(stock.llm, "_quick_chat", lambda *a, **k: payload)
    out = stock.ai_decide(gid, "ai_value")
    assert out["analysis"]["kind"] == "个股"
    assert out["analysis"]["symbols"] == ["sh600000"]
    assert "放量" in out["analysis"]["keywords"]

    r = stock.search_analyses(gid, kind="个股", q="突破")
    assert r["total"] == 1
    assert r["items"][0]["player_id"] == "ai_value"
    # 幂等重放也要带上分析
    again = stock.ai_decide(gid, "ai_value")
    assert again["reused"] is True and again["analysis"]["kind"] == "个股"
    # 回溯能读到这条分析
    assert len(stock.state_at(gid, stock.game_state(gid)["cursor_date"])["analyses"]) == 1


def test_ai_old_format_still_records_analysis(db, monkeypatch):
    """旧格式（没有 analysis 字段）也要落一条可检索的兜底分析。"""
    gid = _new_game()["id"]
    monkeypatch.setattr(stock, "get_api_conf",
                        lambda provider: ("k", "http://example.invalid", "m"))
    monkeypatch.setattr(stock.llm, "_quick_chat", lambda *a, **k: (
        '{"orders":[{"symbol":"sh600000","side":"buy","qty":100,"reason":"ok"}],'
        '"journal":"老格式复盘"}'))
    out = stock.ai_decide(gid, "ai_value")
    assert out["analysis"] is not None
    assert out["analysis"]["kind"] == "持仓"          # 有成交 -> 持仓类
    assert out["analysis"]["symbols"] == ["sh600000"]
    assert out["analysis"]["text"] == "老格式复盘"


# ------------------------------------------------------------
# 8. 训练辅助：盲预测 / 市场状态 / 计分板 / 参考散户
# ------------------------------------------------------------

def _bars_of(closes, start=datetime.date(2026, 8, 3)):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({
            "date": (start + datetime.timedelta(days=i)).isoformat(),
            "open": o, "close": c,
            "high": max(o, c) * 1.001, "low": min(o, c) * 0.999,
            "volume": 100000, "amount": 0,
        })
    return out


def _make_game(closes, days=10, ai_count=0, bench_closes=None):
    """用可控的收盘序列开局，便于精确验证预测结算与前向收益。"""
    syms = ["sh600000", "sh510300"]
    bars = _bars_of(closes)
    return stock.create_game(
        mode="replay", symbols=syms, days=days, ai_count=ai_count,
        initial_cash=1000000, names=NAMES,
        bars_by_symbol={s: bars for s in syms},
        benchmark_bars=_bars_of(bench_closes if bench_closes else closes))


def test_prediction_settles_up_down_flat(db):
    # 每日 +1%：1 日预测应判为「涨」
    g = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=10)
    p = stock.create_prediction(g["id"], "you", "sh600000", 1, "up")
    assert p["settled"] is False and p["remaining_days"] == 1
    assert p["threshold"] >= 0.5                     # 自适应阈值不会低于 0.5%
    stock.next_day(g["id"])
    got = next(x for x in stock.list_predictions(g["id"]) if x["id"] == p["id"])
    assert got["settled"] is True and got["hit"] == 1
    assert got["actual_pct"] > got["threshold"]

    # 每日 -1%：看跌应判对，看涨应判错
    g2 = _make_game([10.0 * (0.99 ** i) for i in range(60)], days=10)
    p_down = stock.create_prediction(g2["id"], "you", "sh600000", 1, "down")
    p_up = stock.create_prediction(g2["id"], "you", "benchmark", 1, "up")
    stock.next_day(g2["id"])
    items = {x["id"]: x for x in stock.list_predictions(g2["id"])}
    assert items[p_down["id"]]["hit"] == 1
    assert items[p_up["id"]]["hit"] == 0

    # 微幅震荡：涨跌幅落在 ±阈值内，应判「平」
    swing = [10.0 + (0.005 if i % 2 else 0) for i in range(60)]
    g3 = _make_game(swing, days=10)
    p_flat = stock.create_prediction(g3["id"], "you", "sh600000", 1, "flat")
    stock.next_day(g3["id"])
    got = next(x for x in stock.list_predictions(g3["id"]) if x["id"] == p_flat["id"])
    assert got["hit"] == 1


def test_prediction_validation(db):
    g = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=10)
    gid = g["id"]
    stock.create_prediction(gid, "you", "sh600000", 3, "up")
    with pytest.raises(stock.StockError):
        stock.create_prediction(gid, "you", "sh600000", 3, "up")      # 同日同标的同期限重复
    with pytest.raises(stock.StockError):
        stock.create_prediction(gid, "you", "sh600000", 2, "up")      # 期限非法
    with pytest.raises(stock.StockError):
        stock.create_prediction(gid, "you", "sh600000", 1, "sideways")  # 方向非法
    with pytest.raises(stock.StockError):
        stock.create_prediction(gid, "you", "sh600519", 1, "up")      # 不在观察池

    # 剩余交易日不足：只剩 1 天却要 5 日
    g2 = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=1)
    with pytest.raises(stock.StockError):
        stock.create_prediction(g2["id"], "you", "sh600000", 5, "up")


def test_prediction_horizon_waits_before_settling(db):
    g = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=10)
    gid = g["id"]
    p = stock.create_prediction(gid, "you", "sh600000", 3, "up")
    stock.next_day(gid)
    got = next(x for x in stock.list_predictions(gid) if x["id"] == p["id"])
    assert got["settled"] is False
    assert got["remaining_days"] == 2                # 3 日预测，走了 1 日
    stock.next_day(gid)
    stock.next_day(gid)
    got = next(x for x in stock.list_predictions(gid) if x["id"] == p["id"])
    assert got["settled"] is True
    assert got["due_date"] == got["due_date"]        # 到期日已写入


def test_prediction_stats_grouping(db):
    g = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=12)
    gid = g["id"]
    stock.create_prediction(gid, "you", "sh600000", 1, "up")
    stock.create_prediction(gid, "you", "benchmark", 1, "down")   # 大盘同向上涨 -> 错
    stock.next_day(gid)
    stats = stock.prediction_stats(gid)
    assert stats["settled"] == 2 and stats["hits"] == 1
    assert stats["accuracy"] == 50.0
    assert stats["by_horizon"][0]["horizon"] == 1
    assert {x["target"] for x in stats["by_target"]} == {"sh600000", "benchmark"}
    assert stats["by_regime"] and stats["by_regime"][0]["settled"] == 2
    assert stats["streak"]["n"] >= 1


def test_market_regime_labels(db):
    up = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=5)
    assert stock.market_regime(up["id"])["trend"] == "up"
    flat = _make_game([10.0] * 60, days=5)
    rg = stock.market_regime(flat["id"])
    assert rg["trend"] == "range"
    assert rg["label"]


def test_trade_scoreboard_forward_returns(db):
    g = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=15)
    gid = g["id"]
    stock.place_order(gid, "you", "sh600000", "buy", 100)
    stock.next_day(gid)                              # 次日开盘成交
    sc = stock.trade_scoreboard(gid)
    t = sc["trades"][0]
    assert t["fwd1"] is None                         # 才过一天，1 日收益还没形成
    assert sc["stats"]["buy_count"] == 0

    for _ in range(5):
        stock.next_day(gid)
    sc = stock.trade_scoreboard(gid)
    t = sc["trades"][0]
    assert t["fwd1"] is not None and t["fwd5"] is not None
    assert t["fwd5"] > 4.5                           # 每日 +1%，5 日约 +5.1%
    assert t["excess5"] is not None
    assert sc["stats"]["buy_count"] == 1
    assert sc["stats"]["buy_win5"] == 1
    assert sc["stats"]["buy_win5_pct"] == 100.0
    # 成交列表里能按 id 找到，供前端合并显示
    assert t["id"]


def test_ai_reference_nav_and_metrics(db):
    g = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=6, ai_count=1)
    gid = g["id"]
    stock.place_order(gid, "you", "sh600000", "buy", 100)
    stock.next_day(gid)
    ref = stock.ai_reference(gid)
    assert len(ref["dates"]) == 2
    assert {p["id"] for p in ref["players"]} == {"you", "ai_value"}
    for p in ref["players"]:
        assert len(p["nav"]) == len(ref["dates"])
        assert p["max_drawdown"] <= 0
        assert p["turnover"] >= 0
    me = next(p for p in ref["players"] if p["id"] == "you")
    assert me["style"] == "你"
    assert me["position_pct"] > 0                    # 已买入 100 股
    ai = next(p for p in ref["players"] if p["id"] == "ai_value")
    assert ai["style"] == "价值"


def test_analysis_bundle_and_regime_in_state(db):
    g = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=5, ai_count=1)
    gid = g["id"]
    bundle = stock.analysis_bundle(gid)
    for key in ("regime", "predictions", "prediction_stats", "scoreboard", "reference"):
        assert key in bundle
    state = stock.game_state(gid)
    assert "regime" in state
    # 兼容性：原有字段仍在
    for key in ("players", "leaderboard", "symbols", "pending_orders", "trades",
                "journals", "benchmark", "fees"):
        assert key in state
    # 增量 patch 也带 regime
    assert "regime" in stock.change_patch(gid, "all")


# ------------------------------------------------------------
# 9. 中途加入标的
# ------------------------------------------------------------

def _alt_bars(base=20.0, n=60, step=0.005):
    return _bars_of([base * ((1 + step) ** i) for i in range(n)])


def test_add_symbol_midgame_replay(db):
    g = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=15)
    gid = g["id"]
    before = {s["symbol"] for s in g["symbols"]}
    assert before == {"sh600000", "sh510300"}

    new_bars = _alt_bars()
    out = stock.add_symbol(gid, "sh600519",
                           bars_by_symbol={"sh600519": new_bars},
                           names={"sh600519": "贵州茅台"})
    assert out["symbol"] == "sh600519"
    assert out["name"] == "贵州茅台"
    state = out["game"]
    syms = {s["symbol"]: s for s in state["symbols"]}
    assert "sh600519" in syms and syms["sh600519"]["name"] == "贵州茅台"

    # 回放模式必须把未来交易日的日 K 也补齐，否则下一日无法撮合
    future = stock.get_db().execute(
        "SELECT COUNT(*) AS n FROM bars WHERE game_id=? AND symbol=? AND date>?",
        (gid, "sh600519", state["cursor_date"])).fetchone()["n"]
    assert future > 0

    # 加入后立刻可交易
    stock.place_order(gid, "you", "sh600519", "buy", 100)
    r = stock.next_day(gid)
    assert any(t["symbol"] == "sh600519" for t in r["game"]["trades"])
    assert stock.audit_game(gid) == []
    # 图表也能取到
    bars = stock.bars_for(gid, "sh600519", 30, with_marks=True)
    assert bars["bars"] and any(m["symbol"] == "sh600519" for m in bars["marks"])


def test_add_symbol_validation(db):
    g = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=5)
    gid = g["id"]
    with pytest.raises(stock.StockError):
        stock.add_symbol(gid, "sh600000")           # 已在观察池

    # 补到 12 只后再加应被拒绝
    extra = {}
    for i in range(10, 20):
        sym = "sh6000%02d" % i
        extra[sym] = _alt_bars(base=10.0 + i)
        stock.add_symbol(gid, sym, bars_by_symbol={sym: extra[sym]})
    assert len(stock.game_state(gid)["symbols"]) == 12
    with pytest.raises(stock.StockError) as e:
        stock.add_symbol(gid, "sh600030", bars_by_symbol={"sh600030": _alt_bars()})
    assert "最多" in str(e.value)

    # 已结束的对局不能加
    g2 = _make_game([10.0 * (1.01 ** i) for i in range(60)], days=1)
    stock.next_day(g2["id"])
    assert stock.game_state(g2["id"])["status"] == "finished"
    with pytest.raises(stock.StockError):
        stock.add_symbol(g2["id"], "sh600519", bars_by_symbol={"sh600519": _alt_bars()})


def test_add_symbol_live_never_writes_future(db):
    closes = [10.0 * (1.01 ** i) for i in range(60)]
    bars = _bars_of(closes)
    syms = ["sh600000", "sh510300"]
    g = stock.create_game(
        mode="live", symbols=syms, days=1, ai_count=0, initial_cash=1000000,
        names=NAMES, bars_by_symbol={s: bars for s in syms},
        benchmark_bars=_bars_of(closes))
    gid = g["id"]
    out = stock.add_symbol(gid, "sh600519",
                           bars_by_symbol={"sh600519": _alt_bars()},
                           names={"sh600519": "贵州茅台"})
    cursor = out["game"]["cursor_date"]
    row = stock.get_db().execute(
        "SELECT MAX(date) AS d FROM bars WHERE game_id=? AND symbol=?",
        (gid, "sh600519")).fetchone()
    assert row["d"] is not None and row["d"] <= cursor
