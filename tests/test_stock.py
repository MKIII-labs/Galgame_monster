# -*- coding: utf-8 -*-
"""功能大厅：模拟股市（行情解析 / 撮合规则 / AI 订单 / 实时推进）。"""
import datetime

import pytest

from app import stock


# ------------------------------------------------------------
# 纯函数
# ------------------------------------------------------------

def test_normalize_symbol():
    assert stock.normalize_symbol("600519") == "sh600519"
    assert stock.normalize_symbol("sh600519") == "sh600519"
    assert stock.normalize_symbol("000001") == "sz000001"
    assert stock.normalize_symbol("300750") == "sz300750"
    assert stock.normalize_symbol("510300") == "sh510300"
    with pytest.raises(stock.StockError):
        stock.normalize_symbol("00700")
    with pytest.raises(stock.StockError):
        stock.normalize_symbol("")


def test_limit_pct():
    assert stock.limit_pct("sh600519", "贵州茅台") == 0.10
    assert stock.limit_pct("sh688981", "中芯国际") == 0.20
    assert stock.limit_pct("sz300750", "宁德时代") == 0.20
    assert stock.limit_pct("sh600519", "ST某某") == 0.05


def test_calc_fee():
    # 买入：佣金 0.025%（最低 5）+ 过户费 0.001%
    assert stock.calc_fee("buy", 10000) == 5.1
    # 卖出：再加印花税 0.05%
    assert stock.calc_fee("sell", 10000) == 10.1
    # 大额：按比例而不是最低佣金
    assert stock.calc_fee("buy", 1000000) == 260.0


def test_drop_partial_today():
    today = datetime.datetime(2026, 8, 3, 10, 0)
    bars = [{"date": "2026-07-31"}, {"date": "2026-08-03"}]
    assert [b["date"] for b in stock._drop_partial_today(bars, now=today)] == ["2026-07-31"]
    after = datetime.datetime(2026, 8, 3, 15, 30)
    assert len(stock._drop_partial_today(bars, now=after)) == 2


def test_extract_json():
    assert stock._extract_json('好的：{"orders":[],"journal":"观望"}')["journal"] == "观望"
    assert stock._extract_json('```json\n{"orders":[],"journal":"x"}\n```')["journal"] == "x"
    assert stock._extract_json("没有 JSON") is None


# ------------------------------------------------------------
# 测试数据 / 临时库
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


def _new_game(days=4, ai_count=1, mode="replay", bars=None, bench=None):
    return stock.create_game(
        mode=mode, symbols=list(BARS), days=days, ai_count=ai_count,
        initial_cash=1000000, names=NAMES,
        bars_by_symbol=bars if bars is not None else BARS,
        benchmark_bars=bench if bench is not None else BENCH,
    )


# ------------------------------------------------------------
# 撮合
# ------------------------------------------------------------

def test_replay_order_t1_and_sell(db):
    g = _new_game()
    gid = g["id"]
    assert g["mode"] == "replay"
    assert g["cursor_date"] < g["next_date"]
    assert g["remaining_days"] == 4
    assert [p["id"] for p in g["players"]] == ["you", "ai_value"]

    stock.place_order(gid, "you", "sh600000", "buy", 100)
    r = stock.next_day(gid)
    human = next(p for p in r["game"]["players"] if p["id"] == "you")
    assert len(human["positions"]) == 1
    pos = human["positions"][0]
    assert pos["qty"] == 100 and pos["available_qty"] == 0       # T+1
    assert human["cash"] < 1000000
    assert len(r["game"]["trades"]) == 1

    # 同一天不能卖
    with pytest.raises(stock.StockError):
        stock.place_order(gid, "you", "sh600000", "sell", 100)

    # 再推进一天，昨天买的变成可卖
    r = stock.next_day(gid)
    human = next(p for p in r["game"]["players"] if p["id"] == "you")
    assert human["positions"][0]["available_qty"] == 100
    stock.place_order(gid, "you", "sh600000", "sell", 100)
    r = stock.next_day(gid)
    human = next(p for p in r["game"]["players"] if p["id"] == "you")
    assert human["positions"] == []
    assert len(r["game"]["trades"]) == 2


def test_order_validation(db):
    gid = _new_game()["id"]
    with pytest.raises(stock.StockError):
        stock.place_order(gid, "you", "sh600000", "buy", 150)      # 非整手
    with pytest.raises(stock.StockError):
        stock.place_order(gid, "you", "sh600519", "buy", 100)      # 不在观察池
    with pytest.raises(stock.StockError):
        stock.place_order(gid, "you", "sh600000", "sell", 100)     # 没有持仓
    with pytest.raises(stock.StockError):
        stock.place_order(gid, "you", "sh600000", "buy", 100000000)  # 现金不足


def test_single_position_cap(db):
    gid = _new_game()["id"]
    # 100 万最多买 35 万；sh600000 约 10 元，4 万股约 40 万，现金够但超过单票上限
    with pytest.raises(stock.StockError) as e:
        stock.place_order(gid, "you", "sh600000", "buy", 40000)
    assert "35%" in str(e.value)

    # 分两笔也不能绕过：1 万股 + 1.1 万股 ≈ 36.3 万
    stock.place_order(gid, "you", "sh600000", "buy", 10000)
    with pytest.raises(stock.StockError) as e2:
        stock.place_order(gid, "you", "sh600000", "buy", 11000)
    assert "35%" in str(e2.value)


def test_no_lookahead_bars(db):
    g = _new_game()
    gid = g["id"]
    bars = stock.bars_for(gid, "sh600000", 100)
    assert bars
    assert all(b["date"] <= g["cursor_date"] for b in bars)
    assert bars[-1]["date"] == g["cursor_date"]
    assert g["next_date"] > g["cursor_date"]


def test_replay_finishes(db):
    gid = _new_game(days=2)["id"]
    stock.next_day(gid)
    r = stock.next_day(gid)
    assert r["game"]["status"] == "finished"
    assert r["game"]["remaining_days"] == 0


# ------------------------------------------------------------
# AI
# ------------------------------------------------------------

def test_ai_decision_and_rejected_orders(db, monkeypatch):
    gid = _new_game()["id"]
    monkeypatch.setattr(stock, "get_api_conf",
                        lambda provider: ("test-key", "http://example.invalid", "model"))
    payload = (
        '{"orders":['
        '{"symbol":"sh510300","side":"buy","qty":100,"reason":"ok"},'
        '{"symbol":"sh510300","side":"sell","qty":100,"reason":"no position"},'
        '{"symbol":"sh600519","side":"buy","qty":100,"reason":"not in pool"},'
        '{"symbol":"sh600000","side":"buy","qty":150,"reason":"lot"}'
        '],"journal":"测试复盘"}'
    )
    monkeypatch.setattr(stock.llm, "_quick_chat",
                        lambda *a, **k: payload)
    out = stock.ai_decide(gid, "ai_value")
    assert len(out["accepted"]) == 1
    assert len(out["rejected"]) == 3
    assert out["journal"] == "测试复盘"

    state = stock.game_state(gid)
    ai = next(p for p in state["players"] if p["id"] == "ai_value")
    assert ai["decided"] is True

    # 幂等：再次调用不重复下单
    again = stock.ai_decide(gid, "ai_value")
    assert again["reused"] is True
    assert len(again["accepted"]) == 1


def test_ai_without_key_reports_clearly(db, monkeypatch):
    gid = _new_game()["id"]
    monkeypatch.setattr(stock, "get_api_conf", lambda provider: ("", "", ""))
    with pytest.raises(stock.StockError) as e:
        stock.ai_decide(gid, "ai_value")
    assert "密钥" in str(e.value)


# ------------------------------------------------------------
# 实时每日盘
# ------------------------------------------------------------

def test_live_wait_for_new_close_and_advance(db):
    g = _new_game(mode="live", days=1, ai_count=0)
    gid = g["id"]
    assert g["mode"] == "live"
    assert g["end_date"] == ""
    assert g["cursor_date"] == BENCH[-1]["date"]

    r = stock.next_day(gid, benchmark_bars=BENCH)
    assert r.get("no_new_day") is True

    nxt = (datetime.date.fromisoformat(BENCH[-1]["date"])
           + datetime.timedelta(days=1)).isoformat()
    nb = [dict(x) for x in BENCH]
    nb.append({"date": nxt, "open": 4008.0, "close": 4012.0,
               "high": 4015.0, "low": 4005.0, "volume": 100000, "amount": 0})
    ns = [dict(x) for x in BARS["sh600000"]]
    ns.append({"date": nxt, "open": 18.0, "close": 18.5,
               "high": 18.8, "low": 17.9, "volume": 100000, "amount": 0})
    r = stock.next_day(gid, benchmark_bars=nb,
                       bars_by_symbol={"sh600000": ns,
                                       "sh510300": BARS["sh510300"]})
    assert r["game"]["cursor_date"] == nxt


# ------------------------------------------------------------
# 列表 / 删除
# ------------------------------------------------------------

def test_list_and_delete(db):
    gid = _new_game()["id"]
    games = stock.list_games()
    assert any(x["id"] == gid for x in games)
    stock.delete_game(gid)
    assert not any(x["id"] == gid for x in stock.list_games())


# ------------------------------------------------------------
# AI prompt 指标缓存
# ------------------------------------------------------------

def test_indicator_cached_reuses_same_day(monkeypatch):
    calls = []

    def fake_indicator(conn, game, symbol):
        calls.append(symbol)
        return "指标-" + symbol

    monkeypatch.setattr(stock, "_indicator_text", fake_indicator)
    stock._IND_CACHE.clear()
    game = {"id": "cache-game", "cursor_date": "2024-01-02"}
    assert stock._indicator_cached(None, game, "sh600000") == "指标-sh600000"
    assert stock._indicator_cached(None, game, "sh600000") == "指标-sh600000"
    assert calls == ["sh600000"]
    # 换一天必须重算，避免把昨天收盘后的指标带进今天
    game = {"id": "cache-game", "cursor_date": "2024-01-03"}
    stock._indicator_cached(None, game, "sh600000")
    assert calls == ["sh600000", "sh600000"]
    stock._IND_CACHE.clear()


# ------------------------------------------------------------
# 多局共存（防回归：玩家 id 固定为 you / ai_value，主键必须带 game_id）
# ------------------------------------------------------------

def test_two_games_can_coexist(db):
    g1 = _new_game(days=2, ai_count=1)
    g2 = _new_game(days=2, ai_count=1)
    assert g1["id"] != g2["id"]

    # 两局各自的玩家互不干扰
    ids1 = {p["id"] for p in stock.game_state(g1["id"])["players"]}
    ids2 = {p["id"] for p in stock.game_state(g2["id"])["players"]}
    assert ids1 == ids2 == {"you", "ai_value"}

    # 第一局继续可玩，且资金独立
    stock.place_order(g1["id"], "you", "sh600000", "buy", 100)
    r1 = stock.next_day(g1["id"])
    assert len(r1["game"]["trades"]) == 1
    r2 = stock.game_state(g2["id"])
    assert r2["trades"] == []
    assert r2["cursor_date"] == g2["cursor_date"]


def test_migrate_legacy_players_pk(tmp_path):
    """旧库（players 主键只有 id）必须能自动迁移，否则第二局开不了。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE games (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',"
        " mode TEXT NOT NULL DEFAULT 'replay', status TEXT NOT NULL DEFAULT 'running',"
        " created_at REAL NOT NULL, start_date TEXT NOT NULL,"
        " end_date TEXT NOT NULL DEFAULT '', cursor_date TEXT NOT NULL,"
        " initial_cash REAL NOT NULL, commission REAL NOT NULL,"
        " min_commission REAL NOT NULL, stamp_duty REAL NOT NULL,"
        " transfer_fee REAL NOT NULL, slippage REAL NOT NULL,"
        " benchmark TEXT NOT NULL DEFAULT 'sh000300');\n"
        "CREATE TABLE players (id TEXT PRIMARY KEY, game_id TEXT NOT NULL,"
        " name TEXT NOT NULL, kind TEXT NOT NULL, persona TEXT NOT NULL DEFAULT '',"
        " cash REAL NOT NULL, initial_cash REAL NOT NULL,"
        " realized_pnl REAL NOT NULL DEFAULT 0);\n"
        "INSERT INTO games(id,created_at,start_date,cursor_date,initial_cash,"
        "commission,min_commission,stamp_duty,transfer_fee,slippage)"
        " VALUES('old_game',1.0,'2026-08-01','2026-08-01',100000,0.00025,5,0.0005,"
        "0.00001,0.001);\n"
        "INSERT INTO players(id,game_id,name,kind,cash,initial_cash)"
        " VALUES('you','old_game','你','human',100000,100000);\n"
    )
    conn.commit()
    conn.close()

    stock.configure(str(path))
    # 迁移后旧数据保留
    old = stock._player(stock.get_db(), "old_game", "you")
    assert old["name"] == "你"

    # 换库后必须还能开新局（旧实现会在这里报 UNIQUE constraint failed）
    stock.reset_store()
    g = _new_game(days=2, ai_count=1)
    assert g["players"][0]["id"] == "you"
