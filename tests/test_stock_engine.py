# -*- coding: utf-8 -*-
"""撮合内核 / 公司行为 / 绩效指标 / 报告导出（stockkit 新增能力）。

与 test_stock.py 的分工：
- test_stock.py 测对局生命周期与旧行为（回归基线，不能改）
- 本文件测**新增**的限价单、部分成交、超时撤单、公司行为、指标与报告
"""
import pytest

from app import stock
from app.stockkit import corporate, matching, report, risk


# ------------------------------------------------------------
# 测试数据（与 test_stock.py 保持同构，便于对照）
# ------------------------------------------------------------

def _dates(n=40, start="2026-08-03"):
    import datetime
    d0 = datetime.date.fromisoformat(start)
    return [(d0 + datetime.timedelta(days=i)).isoformat() for i in range(n)]


def _series(base, n=40, volume=100000):
    out = []
    for i, d in enumerate(_dates(n)):
        o = base + i * 0.2
        c = o + (0.5 if i % 2 == 0 else -0.3)
        out.append({
            "date": d, "open": o, "close": c,
            "high": max(o, c) + 0.4, "low": min(o, c) - 0.4,
            "volume": volume + i * 1000, "amount": 0,
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


def _new_game(days=4, ai_count=0, bars=None, bench=None):
    return stock.create_game(
        mode="replay", symbols=list(BARS), days=days, ai_count=ai_count,
        initial_cash=1000000, names=NAMES,
        bars_by_symbol=bars if bars is not None else BARS,
        benchmark_bars=bench if bench is not None else BENCH,
    )


def _flat_bars(price=10.0, n=40, volume=100000):
    """恒定价格：开=高=低=收，便于精确断言成交价。"""
    return [{"date": d, "open": price, "high": price, "low": price,
             "close": price, "volume": volume, "amount": 0} for d in _dates(n)]


# ------------------------------------------------------------
# 撮合内核（纯函数）
# ------------------------------------------------------------

def test_plan_fill_market_order_uses_open_with_slippage():
    bar = {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1e6}
    plan = matching.plan_fill("buy", 100, bar, slippage=0.001, symbol="sh600000")
    assert plan.filled
    assert plan.qty == 100
    assert plan.price == pytest.approx(10.01)
    assert plan.partial is False


def test_plan_fill_limit_buy_not_touched_stays_pending():
    bar = {"open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 1e6}
    plan = matching.plan_fill("buy", 100, bar, limit_price=9.0, symbol="sh600000")
    # 没触价：qty=0 且没有判废原因 → 明天继续挂
    assert plan.qty == 0
    assert plan.reason == ""


def test_plan_fill_limit_buy_touched_intraday_fills_at_limit():
    bar = {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1e6}
    plan = matching.plan_fill("buy", 100, bar, limit_price=9.2, symbol="sh600000")
    assert plan.filled
    assert plan.price == pytest.approx(9.2)


def test_plan_fill_partial_by_participation():
    # 当日成交量 1000 → 10% = 100 股上限，买 1000 股只能成 100 股
    bar = {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1000}
    plan = matching.plan_fill("buy", 1000, bar, symbol="sh600000")
    assert plan.filled
    assert plan.qty == 100
    assert plan.partial is True


def test_plan_fill_suspended_rejects_market_but_keeps_limit():
    bar = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0}
    market = matching.plan_fill("buy", 100, bar, symbol="sh600000")
    assert market.qty == 0 and market.reason          # 市价单直接判废
    limit = matching.plan_fill("buy", 100, bar, limit_price=9.0, symbol="sh600000")
    assert limit.qty == 0 and limit.reason == ""      # 限价单继续等


def test_plan_fill_limit_up_blocks_buy():
    bar = {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1e6}
    plan = matching.plan_fill("buy", 100, bar, prev_close=10.0, limit=0.10,
                              symbol="sh600000")
    assert plan.qty == 0
    assert "涨停" in plan.reason


def test_plan_fill_pending_timeout():
    bar = {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1e6}
    plan = matching.plan_fill("buy", 100, bar, limit_price=1.0,
                              pending_days=matching.MAX_PENDING_DAYS,
                              symbol="sh600000")
    assert plan.qty == 0
    assert "自动撤销" in plan.reason


def test_cap_by_volume_respects_lot_and_crypto_decimals():
    # A 股按 100 股整手向下取整
    assert matching.cap_by_volume(1050, "sh600000", 1e9) == 1000
    # 币市按 1e-6 向下取整，不能按整手砍
    got = matching.cap_by_volume(0.1234567, "btcusdt", 1e9)
    assert got == pytest.approx(0.123456, abs=1e-9)


# ------------------------------------------------------------
# 限价单接入对局（端到端）
# ------------------------------------------------------------

def test_limit_order_waits_then_fills(db):
    flat = _flat_bars(10.0)
    gid = _new_game(bars={"sh600000": flat, "sh510300": flat},
                    bench=_series(4000.0))["id"]
    # 限价 9.0 明显低于 10.0，第一天不成交
    order = stock.place_order(gid, "you", "sh600000", "buy", 100, limit_price=9.0)
    assert order["limit_price"] == pytest.approx(9.0)
    r = stock.next_day(gid)
    human = next(p for p in r["game"]["players"] if p["id"] == "you")
    assert human["positions"] == []                      # 没成交
    pend = r["game"]["pending_orders"]
    assert [o["id"] for o in pend] == [order["id"]]      # 还挂着
    assert pend[0]["pending_days"] == 1


def test_market_order_still_fills_next_day(db):
    gid = _new_game()["id"]
    stock.place_order(gid, "you", "sh600000", "buy", 100)
    r = stock.next_day(gid)
    human = next(p for p in r["game"]["players"] if p["id"] == "you")
    assert len(human["positions"]) == 1


def test_partial_fill_keeps_order_pending_and_accumulates(db):
    # 日成交量压到 1000 → 参与率上限 100 股，而委托 1000 股
    thin = _flat_bars(10.0, volume=1000)
    gid = _new_game(bars={"sh600000": thin, "sh510300": thin},
                    bench=_series(4000.0))["id"]
    oid = stock.place_order(gid, "you", "sh600000", "buy", 1000)["id"]
    r = stock.next_day(gid)
    human = next(p for p in r["game"]["players"] if p["id"] == "you")
    assert human["positions"][0]["qty"] == 100           # 只成了 100 股
    # 委托没结束，继续挂着等明天
    assert any(o["id"] == oid for o in r["game"]["pending_orders"])


def test_adjusted_bars_returns_history(db):
    gid = _new_game()["id"]
    bars = stock.adjusted_bars(gid, "sh600000", limit=30)
    assert len(bars) > 0
    assert "close" in bars[0]


# ------------------------------------------------------------
# 公司行为
# ------------------------------------------------------------

def test_corporate_normalize_action_rejects_unknown_and_crypto():
    with pytest.raises(stock.StockError):
        corporate.normalize_action({"kind": "unknown"})
    with pytest.raises(stock.StockError):
        corporate.normalize_action({"kind": "dividend", "per_10": 1}, "btcusdt")


def test_corporate_split_position_keeps_cost_basis():
    qty, avg = corporate.split_position(100, 10.0, 2.0)
    assert qty == 200
    assert avg == pytest.approx(5.0)         # 成本额不变


def test_corporate_dividend_cash():
    assert corporate.dividend_cash(1000, 0.3) == pytest.approx(300.0)
    assert corporate.dividend_cash(1000, -1) == 0.0     # 负数按 0


def test_corporate_adjust_history_halves_prior_prices():
    bars = [{"date": "2026-08-03", "open": 10, "high": 10, "low": 10,
             "close": 10, "volume": 100},
            {"date": "2026-08-04", "open": 20, "high": 20, "low": 20,
             "close": 20, "volume": 100}]
    act = {"kind": "split", "ratio": 2.0, "ex_date": "2026-08-04"}
    out = corporate.adjust_history(bars, act)
    assert out[0]["close"] == pytest.approx(5.0)        # 除权日前折算
    assert out[0]["volume"] == pytest.approx(200)       # 成交量反向折算
    assert out[1]["close"] == pytest.approx(20.0)       # 除权日不动
    # 原对象不被修改
    assert bars[0]["close"] == 10


def test_apply_corporate_action_dividend_credits_cash(db):
    gid = _new_game()["id"]
    stock.place_order(gid, "you", "sh600000", "buy", 1000)
    stock.next_day(gid)
    before = stock.game_state(gid)
    cash_before = next(p for p in before["players"] if p["id"] == "you")["cash"]

    out = stock.apply_corporate_action(
        gid, "sh600000", {"kind": "dividend", "per_10": 5.0})
    assert out["players"] == 1
    assert out["cash_total"] == pytest.approx(500.0)     # 1000 股 × 0.5 元

    after = stock.game_state(gid)
    cash_after = next(p for p in after["players"] if p["id"] == "you")["cash"]
    assert cash_after == pytest.approx(cash_before + 500.0)


def test_apply_corporate_action_split_doubles_qty(db):
    gid = _new_game()["id"]
    stock.place_order(gid, "you", "sh600000", "buy", 1000)
    stock.next_day(gid)
    out = stock.apply_corporate_action(
        gid, "sh600000", {"kind": "split", "per_10": 10.0})
    assert out["kind"] == "split"
    state = stock.game_state(gid)
    pos = next(p for p in state["players"] if p["id"] == "you")["positions"][0]
    assert pos["qty"] == 2000                            # 10 送 10


# ------------------------------------------------------------
# 绩效指标 / 报告
# ------------------------------------------------------------

def test_risk_max_drawdown_and_volatility():
    vals = [100.0, 120.0, 90.0, 110.0]
    assert risk.max_drawdown(vals) == pytest.approx(25.0)   # 120 → 90
    assert risk.annualized_volatility(vals) > 0


def test_risk_win_rate_and_profit_factor():
    trades = [
        {"side": "sell", "realized_pnl": 100.0},
        {"side": "sell", "realized_pnl": -50.0},
        {"side": "buy", "realized_pnl": 999.0},     # 买入不计入平仓
    ]
    assert risk.win_rate(trades) == pytest.approx(50.0)
    assert risk.profit_factor(trades) == pytest.approx(2.0)


def test_risk_metrics_on_live_game(db):
    gid = _new_game()["id"]
    stock.place_order(gid, "you", "sh600000", "buy", 100)
    stock.next_day(gid)
    m = stock.risk_metrics(gid, "you")
    assert m["player_id"] == "you"
    assert "max_drawdown" in m and "sharpe" in m
    assert m["days"] >= 1


def test_export_report_markdown_and_csv(db):
    gid = _new_game()["id"]
    stock.place_order(gid, "you", "sh600000", "buy", 100)
    stock.next_day(gid)

    md = stock.export_report(gid, "markdown")
    assert md["filename"].endswith(".md")
    assert "模拟股市报告" in md["text"]
    assert "回撤" in md["text"]

    csv_out = stock.export_report(gid, "csv")
    assert csv_out["filename"].endswith(".csv")
    assert "side" in csv_out["text"]

    with pytest.raises(ValueError):
        report.render({}, "pdf")


# ------------------------------------------------------------
# 旧接口兼容（门面没变）
# ------------------------------------------------------------

def test_facade_still_exposes_legacy_names():
    for name in ("create_game", "game_state", "place_order", "cancel_order",
                 "next_day", "bars_for", "trades_for", "search_analyses",
                 "ai_decide", "audit_game", "change_patch", "state_at",
                 "compare_states", "timeline_dates", "create_prediction",
                 "analysis_bundle", "add_symbol", "archive_analyses"):
        assert callable(getattr(stock, name)), name
