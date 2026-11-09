# -*- coding: utf-8 -*-
"""组合风险指标：最大回撤、年化波动、胜率、盈亏比、夏普、超额收益。

全部是纯函数，输入是「已经算好的数列」，不碰数据库也不碰网络 ——
这样这些指标可以独立验证，也能复用到别的模拟盘（数学竞技场等）。

口径说明（写清楚，避免以后对着数字猜）：

- 最大回撤：逐日总资产序列上的 ``max((峰值 - 当日) / 峰值)``，正数百分数。
  用**收盘估值**（snapshots），不含日内回撤。
- 年化波动：日收益率样本标准差 × sqrt(252)，正数百分数。
  样本不足 2 个时返回 0（样本标准差对 1 个点无定义，硬凑一个 0 会误导）。
- 胜率：``realized_pnl > 0`` 的**平仓笔数**占比。只统计卖出成交，
  买入不产生已实现盈亏。清仓式卖出才算一笔，不按天算。
- 盈亏比：总盈利 / 总亏损（都是正数），无亏损笔时返回 0 并在
  ``note`` 里说明，避免返回 inf 让 JSON 序列化炸掉。
- 夏普：``mean(daily) / stdev(daily) * sqrt(252)``，无风险利率按 0 处理
  （模拟盘不做无风险套利，简化是合理的）。
- 超额收益：组合总收益率 - 基准总收益率，正数百分数。

所有返回值都已 round 到 2 位，前端直接拼百分号即可。
"""
import math
import statistics

TRADING_DAYS = 252      # A 股一年约 244~250 个交易日，业界习惯用 252
EPS = 1e-12


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def equity_values(rows):
    """从 snapshots / 视图里抽出逐日总资产序列（按日期升序）。

    rows 允许是 dict 或 sqlite3.Row；缺 equity 字段时退回 cash+market_value。
    """
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            v = r.get("equity")
            if v is None:
                v = _num(r.get("cash")) + _num(r.get("market_value"))
            out.append(_num(v))
        else:
            try:
                out.append(_num(r["equity"]))
            except (IndexError, KeyError, TypeError):
                continue
    return out


def max_drawdown(values):
    """最大回撤（正数百分数）。空序列 / 全 0 返回 0。"""
    peak = None
    worst = 0.0
    for v in values or []:
        v = _num(v)
        if peak is None or v > peak:
            peak = v
        if peak and peak > EPS:
            dd = (peak - v) / peak * 100.0
            if dd > worst:
                worst = dd
    return round(worst, 2)


def daily_returns(values):
    """逐日收益率序列（小数）。前一个点非正时跳过该点。"""
    out = []
    prev = None
    for v in values or []:
        v = _num(v)
        if prev is not None and prev > EPS:
            out.append(v / prev - 1.0)
        if v > EPS:
            prev = v
    return out


def annualized_volatility(values, periods=TRADING_DAYS):
    """年化波动率（正数百分数）。样本 < 2 返回 0。"""
    rets = daily_returns(values)
    if len(rets) < 2:
        return 0.0
    return round(statistics.stdev(rets) * math.sqrt(float(periods)) * 100.0, 2)


def sharpe(values, periods=TRADING_DAYS):
    """夏普比率（无风险利率按 0）。样本 < 2 或零波动返回 0。"""
    rets = daily_returns(values)
    if len(rets) < 2:
        return 0.0
    sd = statistics.stdev(rets)
    if sd <= EPS:
        return 0.0
    return round(statistics.mean(rets) / sd * math.sqrt(float(periods)), 2)


def closed_trades(trades):
    """只保留平仓笔（卖出且实现盈亏字段存在）。"""
    out = []
    for t in trades or []:
        side = str(t.get("side") or "") if isinstance(t, dict) else str(t["side"] or "")
        if side != "sell":
            continue
        out.append(t)
    return out


def _pnl(t):
    if isinstance(t, dict):
        return _num(t.get("realized_pnl"))
    try:
        return _num(t["realized_pnl"])
    except (IndexError, KeyError, TypeError):
        return 0.0


def win_rate(trades):
    """胜率（正数百分数，按平仓笔数）。无平仓笔返回 0。"""
    rows = closed_trades(trades)
    if not rows:
        return 0.0
    wins = sum(1 for t in rows if _pnl(t) > 0)
    return round(wins / len(rows) * 100.0, 2)


def profit_factor(trades):
    """盈亏比 = 总盈利 / 总亏损。无亏损笔时返回 0（不返回 inf）。"""
    rows = closed_trades(trades)
    gain = sum(_pnl(t) for t in rows if _pnl(t) > 0)
    loss = -sum(_pnl(t) for t in rows if _pnl(t) < 0)
    if loss <= EPS:
        return 0.0
    return round(gain / loss, 2)


def excess_return(total_return_pct, benchmark_return_pct):
    """超额收益（正数/负数百分数，保留符号）。"""
    return round(_num(total_return_pct) - _num(benchmark_return_pct), 2)


def summary(values, trades=None, total_return_pct=0.0, benchmark_return_pct=0.0):
    """一次性算出全部指标，返回扁平 dict（便于直接塞进 JSON / 提示词）。

    ``values`` 是逐日总资产序列，``trades`` 是该玩家的成交记录。
    """
    vals = [_num(v) for v in (values or [])]
    rows = closed_trades(trades)
    gain = sum(_pnl(t) for t in rows if _pnl(t) > 0)
    loss = -sum(_pnl(t) for t in rows if _pnl(t) < 0)
    return {
        "days": len(vals),
        "max_drawdown": max_drawdown(vals),
        "annualized_volatility": annualized_volatility(vals),
        "sharpe": sharpe(vals),
        "win_rate": win_rate(trades),
        "profit_factor": profit_factor(trades),
        "closed_trades": len(rows),
        "gross_profit": round(gain, 2),
        "gross_loss": round(loss, 2),
        "excess_return": excess_return(total_return_pct, benchmark_return_pct),
        "total_return": round(_num(total_return_pct), 2),
        "benchmark_return": round(_num(benchmark_return_pct), 2),
    }
