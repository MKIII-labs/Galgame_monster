# -*- coding: utf-8 -*-
"""币市：符号、费用、无 T+1 / 无涨跌停、AI 提示词。"""
import datetime

import pytest

from app import stock


def _dates(n=40, start=datetime.date(2026, 8, 3)):
    return [(start + datetime.timedelta(days=i)).isoformat() for i in range(n)]


def _series(base, n=40):
    out = []
    for i, d in enumerate(_dates(n)):
        o = base + i * base * 0.001
        c = o + base * 0.002
        out.append({"date": d, "open": o, "close": c,
                    "high": max(o, c) * 1.01, "low": min(o, c) * 0.99,
                    "volume": 1000 + i * 10, "amount": 0})
    return out


BARS = {"BTCUSDT": _series(60000), "ETHUSDT": _series(3000), "SOLUSDT": _series(150)}
BENCH = _series(60000)
NAMES = {"BTCUSDT": "比特币", "ETHUSDT": "以太坊", "SOLUSDT": "Solana"}


@pytest.fixture
def db(tmp_path):
    stock.configure(str(tmp_path / "stock.db"))
    stock.reset_store()
    yield stock
    stock.reset_store()


def _new(days=4, ai_count=1):
    return stock.create_game(
        mode="replay", market="crypto", symbols=list(BARS), days=days,
        ai_count=ai_count, initial_cash=1000000, names=NAMES,
        bars_by_symbol=BARS, benchmark_bars=BENCH)


def test_crypto_symbols_and_rules():
    assert stock.is_crypto("btcusdt")
    assert stock.normalize_symbol("btcusdt") == "BTCUSDT"
    assert stock.symbol_code("BTCUSDT") == "BTC"
    assert stock.lot_size("BTCUSDT") == 0
    assert stock.limit_pct("BTCUSDT") == 0.0
    assert stock.is_etf("BTCUSDT") is False
    assert stock.search_symbols("BTC")[0]["symbol"] == "BTCUSDT"


def test_create_game_marks_crypto(db):
    g = _new()
    assert g["market"] == "crypto"
    assert g["benchmark"]["symbol"] == "BTCUSDT"
    assert g["fees"]["commission"] == pytest.approx(0.001)
    assert g["fees"]["stamp_duty"] == 0
    syms = {s["symbol"] for s in g["symbols"]}
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT"} <= syms


def test_fractional_buy_has_no_t1(db):
    gid = _new()["id"]
    stock.place_order(gid, "you", "BTCUSDT", "buy", 0.5)
    r = stock.next_day(gid)
    human = next(p for p in r["game"]["players"] if p["id"] == "you")
    pos = human["positions"][0]
    assert pos["qty"] == pytest.approx(0.5)
    assert pos["available_qty"] == pytest.approx(0.5)     # 币市买入即可卖

    # 同一天就能卖（股票这里会报 T+1）
    stock.place_order(gid, "you", "BTCUSDT", "sell", 0.5)
    r = stock.next_day(gid)
    human = next(p for p in r["game"]["players"] if p["id"] == "you")
    assert human["positions"] == []


def test_state_at_has_no_t1_for_crypto(db):
    """历史回溯也走同一套口径：币市买入当天即可卖，不能显示成 T+1。"""
    gid = _new()["id"]
    stock.place_order(gid, "you", "BTCUSDT", "buy", 0.5)
    r = stock.next_day(gid)
    day = r["game"]["cursor_date"]
    st = stock.state_at(gid, day)
    me = next(p for p in st["players"] if p["kind"] == "human")
    pos = me["positions"][0]
    assert pos["qty"] == pytest.approx(0.5)
    assert pos["available_qty"] == pytest.approx(0.5)


def test_reject_mixed_market(db):
    with pytest.raises(stock.StockError):
        stock.create_game(market="crypto", symbols=["BTCUSDT", "sh600000"],
                          days=4, bars_by_symbol=BARS, benchmark_bars=BENCH)
    with pytest.raises(stock.StockError):
        stock.create_game(market="stock", symbols=["BTCUSDT", "ETHUSDT"],
                          days=4, bars_by_symbol=BARS, benchmark_bars=BENCH)


def test_add_symbol_respects_market(db):
    gid = _new()["id"]
    with pytest.raises(stock.StockError):
        stock.add_symbol(gid, "sh600000", bars_by_symbol=BARS)


def test_ai_prompt_is_crypto_aware(db):
    gid = _new()["id"]
    conn = stock.get_db()
    game = stock._game(conn, gid)
    player = stock._player(conn, gid, "ai_value")
    text = stock._build_ai_prompt(conn, game, player)
    assert "币市" in text
    assert "USDT" in text
    assert "0.000001" in text
    assert "BTCUSDT" in text
    assert "无 T+1" in text
    assert "100 股" not in text
