# -*- coding: utf-8 -*-
"""对局报告导出：Markdown / CSV。

只做「把已经算好的 dict 排版」，不查库、不读文件 ——
格式可以脱离 SQLite 单测，API 层也能直接复用同一份输出。

输入约定：``state`` 是 ``app/stock.stock.game_state(game_id)`` 的返回结构：

    {"game": {...}, "players": [...], "trades": [...], "bars": [...], ...}

字段缺失一律按空值处理，不抛异常：报告是「尽量导出」，不该因为某个
可选字段被裁掉就整份失败。
"""

import csv
import io

CSV_COLUMNS = ("date", "player_id", "symbol", "side", "qty", "price",
               "amount", "fee", "realized_pnl")


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fmt_money(v, unit="元"):
    return "%.2f %s" % (_f(v), unit)


def fmt_pct(v):
    return "%+.2f%%" % _f(v)


def _unit_of(game):
    return "USDT" if str((game or {}).get("market") or "") == "crypto" else "元"


def _player_rows(players):
    out = []
    for p in players or []:
        out.append({
            "id": p.get("id") or "",
            "name": p.get("name") or p.get("id") or "",
            "kind": p.get("kind") or "",
            "cash": _f(p.get("cash")),
            "equity": _f(p.get("equity"), _f(p.get("cash"))),
            "pnl": _f(p.get("pnl")),
            "return_pct": _f(p.get("return_pct")),
            "positions": len(p.get("positions") or []),
        })
    return out


def _trade_rows(trades):
    out = []
    for t in trades or []:
        out.append({k: t.get(k, "") for k in CSV_COLUMNS})
    return out


def render_markdown(state):
    """把一局对局渲染成 Markdown 报告（标题 + 概览表 + 持仓 + 成交）。"""
    state = state or {}
    game = state.get("game") or {}
    unit = _unit_of(game)
    lines = []
    title = game.get("name") or game.get("id") or "模拟对局"
    lines.append("# 模拟股市报告：%s" % title)
    lines.append("")
    lines.append("- 模拟日：%s" % (game.get("cursor_date") or "—"))
    lines.append("- 市场：%s" % ("币市" if unit == "USDT" else "A 股"))
    lines.append("- 状态：%s" % (game.get("status") or "—"))
    if game.get("benchmark"):
        bm = game["benchmark"]
        lines.append("- 基准：%s %s（%s）" % (
            bm.get("name") or "", bm.get("symbol") or "",
            fmt_pct(bm.get("return_pct"))))
    if state.get("market_state"):
        ms = state["market_state"]
        lines.append("- 行情状态：%s（%s）" % (
            ms.get("state") or "—", ms.get("hint") or ""))
    lines.append("")

    rows = _player_rows(state.get("players"))
    if rows:
        lines.append("## 参与者")
        lines.append("")
        lines.append("| 参与者 | 身份 | 现金 | 总资产 | 盈亏 | 收益率 | 持仓数 |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
        for r in rows:
            lines.append("| %s | %s | %s | %s | %s | %s | %d |" % (
                r["name"], r["kind"], fmt_money(r["cash"], unit),
                fmt_money(r["equity"], unit), fmt_money(r["pnl"], unit),
                fmt_pct(r["return_pct"]), r["positions"]))
        lines.append("")

    holdings = []
    for p in state.get("players") or []:
        for pos in p.get("positions") or []:
            holdings.append({
                "player": p.get("name") or p.get("id") or "",
                "symbol": pos.get("symbol") or "",
                "name": pos.get("name") or pos.get("symbol") or "",
                "qty": _f(pos.get("qty")),
                "available": _f(pos.get("available_qty")),
                "avg_cost": _f(pos.get("avg_cost")),
                "last": _f(pos.get("last")),
                "pnl": _f(pos.get("pnl")),
                "pnl_pct": _f(pos.get("pnl_pct")),
            })
    if holdings:
        lines.append("## 持仓")
        lines.append("")
        lines.append("| 参与者 | 标的 | 数量 | 可卖 | 成本 | 现价 | 浮动盈亏 | 盈亏率 |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for h in holdings:
            lines.append("| %s | %s %s | %g | %g | %s | %s | %s | %s |" % (
                h["player"], h["symbol"], h["name"], h["qty"], h["available"],
                fmt_money(h["avg_cost"], unit), fmt_money(h["last"], unit),
                fmt_money(h["pnl"], unit), fmt_pct(h["pnl_pct"])))
        lines.append("")

    trades = _trade_rows(state.get("trades"))
    if trades:
        lines.append("## 成交记录（最近 %d 笔）" % len(trades))
        lines.append("")
        lines.append("| 日期 | 参与者 | 标的 | 方向 | 数量 | 价格 | 金额 | 费用 | 实现盈亏 |")
        lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |")
        for t in trades:
            side = "买入" if t.get("side") == "buy" else "卖出"
            lines.append("| %s | %s | %s | %s | %g | %s | %s | %s | %s |" % (
                t.get("date") or "", t.get("player_id") or "", t.get("symbol") or "",
                side, _f(t.get("qty")), fmt_money(t.get("price"), unit),
                fmt_money(t.get("amount"), unit), fmt_money(t.get("fee"), unit),
                fmt_money(t.get("realized_pnl"), unit)))
        lines.append("")

    risk = state.get("risk")
    if isinstance(risk, dict) and risk:
        lines.append("## 风险指标")
        lines.append("")
        lines.append("- 最大回撤：%.2f%%" % _f(risk.get("max_drawdown")))
        lines.append("- 年化波动：%.2f%%" % _f(risk.get("annualized_volatility")))
        lines.append("- 夏普：%.2f" % _f(risk.get("sharpe")))
        lines.append("- 胜率：%.2f%%" % _f(risk.get("win_rate")))
        lines.append("- 盈亏比：%.2f" % _f(risk.get("profit_factor")))
        lines.append("- 超额收益：%s" % fmt_pct(risk.get("excess_return")))
        lines.append("")

    if not rows and not trades:
        lines.append("_这一局还没有参与者的成交记录。_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_csv(trades):
    """成交记录导出成 CSV 文本（带表头，UTF-8，无 BOM）。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for t in trades or []:
        writer.writerow({k: ("" if t.get(k) is None else t.get(k)) for k in CSV_COLUMNS})
    return buf.getvalue()


def render(state, fmt="markdown"):
    """统一入口：``fmt`` 取 ``markdown`` / ``csv``。"""
    f = str(fmt or "markdown").strip().lower()
    if f in ("md", "markdown"):
        return render_markdown(state)
    if f == "csv":
        return render_csv((state or {}).get("trades"))
    raise ValueError("不支持的报告格式：%s" % fmt)


def filename(state, fmt="markdown"):
    """建议文件名（不含路径）：``report_<对局id>_<模拟日>.<ext>``。"""
    game = (state or {}).get("game") or {}
    gid = str(game.get("id") or "game").replace("/", "_")
    day = str(game.get("cursor_date") or "").replace("-", "")
    ext = "csv" if str(fmt or "").lower() == "csv" else "md"
    return "report_%s%s.%s" % (gid, ("_" + day) if day else "", ext)
