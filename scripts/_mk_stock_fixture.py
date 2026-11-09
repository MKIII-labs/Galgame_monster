"""生成前端渲染自检用的 fixture：真实跑一局模拟股市，导出 state / bars / risk。

只读脚本，产物写 tests/fixtures/stock_ui.json，供 web/_stock_preview.html 喂给前端。
不联网：用 bars_by_symbol 注入口造 K 线，波动用固定种子的随机游走。
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import stock  # noqa: E402

SYMBOLS = ["sh600519", "sz000001", "sh510300", "sz300750"]
NAMES = {
    "sh600519": "贵州茅台",
    "sz000001": "平安银行",
    "sh510300": "沪深300ETF",
    "sz300750": "宁德时代",
}


def _series(base, n, seed):
    rng = random.Random(seed)
    out = []
    price = base
    for i in range(n):
        drift = rng.uniform(-0.022, 0.024)
        o = price
        c = max(0.5, price * (1 + drift))
        hi = max(o, c) * (1 + rng.uniform(0, 0.012))
        lo = min(o, c) * (1 - rng.uniform(0, 0.012))
        out.append({
            "date": "2026-%02d-%02d" % (1 + (i // 28), 1 + (i % 28)),
            "open": round(o, 2), "close": round(c, 2),
            "high": round(hi, 2), "low": round(lo, 2),
            "volume": rng.randint(60000, 220000),
            "amount": round(c * rng.randint(60000, 220000), 2),
        })
        price = c
    return out


def main():
    days = 20
    # 用临时库，绝不碰真实 data/stock.db
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fixture_stock.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    stock.configure(db_path)

    bars = {}
    for i, s in enumerate(SYMBOLS):
        bars[s] = _series(10.0 + i * 37, days + 150, seed=100 + i)
    bench = _series(4000.0, days + 150, seed=999)

    game = stock.create_game(
        mode="replay", symbols=SYMBOLS, days=days, ai_count=3,
        initial_cash=1000000, names=NAMES,
        bars_by_symbol=bars, benchmark_bars=bench,
    )
    gid = game["id"]

    # 造几笔成交，好让 K 线上有买卖标记
    for _ in range(4):
        stock.next_day(gid, bars_by_symbol=bars, benchmark_bars=bench)

    def buy(sym, qty, limit=0):
        try:
            stock.place_order(gid, "you", sym, "buy", qty, reason="fixture", limit_price=limit)
        except Exception as e:  # noqa: BLE001  下单可能因资金/涨跌停失败，忽略
            print("  跳过下单 %s %s: %s" % (sym, qty, e))

    buy("sh600519", 100)
    stock.next_day(gid, bars_by_symbol=bars, benchmark_bars=bench)
    buy("sz000001", 2000)
    stock.next_day(gid, bars_by_symbol=bars, benchmark_bars=bench)
    buy("sz300750", 300)
    stock.next_day(gid, bars_by_symbol=bars, benchmark_bars=bench)
    # 一笔限价挂单（挂在偏低价格，保持 pending，验证待成交列表）
    buy("sh510300", 1000, limit=round(bars["sh510300"][-1]["close"] * 0.85, 2))
    stock.next_day(gid, bars_by_symbol=bars, benchmark_bars=bench)

    state = stock.game_state(gid)
    # 每只标的都取一份 bars（含成交标记）
    bars_out = {}
    for s in state["symbols"]:
        bars_out[s["symbol"]] = stock.bars_for(gid, s["symbol"], limit=90, with_marks=True)
    risk = stock.risk_summary(gid) if hasattr(stock, "risk_summary") else {}
    report = stock.export_report(gid, fmt="markdown")

    out = {"state": state, "bars": bars_out, "risk": risk, "report": report}
    dst = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tests", "fixtures", "stock_ui.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("fixture -> %s (%d bytes)" % (dst, os.path.getsize(dst)))
    print("对局 %s  cursor=%s  标的=%d  持仓=%d  成交=%d  待成交=%d" % (
        gid, state.get("cursor_date"), len(state.get("symbols") or []),
        len((state.get("players") or [{}])[0].get("positions") or []),
        len(state.get("trades") or []), len(state.get("pending_orders") or [])))
    try:
        os.remove(db_path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
