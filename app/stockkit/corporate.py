# -*- coding: utf-8 -*-
"""公司行为：现金分红、送转 / 拆股、停复牌。

纯函数：输入「持仓 / 日 K 序列 + 一条公司行为」，输出调整后的持仓与除权后的历史。
不碰数据库，调用方（app/stock.py）负责落库。

**为什么需要它**：除权除息日 A 股会价格跳空，但持仓的数量与成本必须同步调整，
否则账户会凭空亏掉一块（分红没入账）或多出一截（送转没加股），
并且这段跳空会被风险指标当成真实回撤。这是模拟盘「账算得平」的前提。

口径（简化，写明避免以后对着数字猜）：

- 现金分红：``每 10 股派 X 元`` -> 每股 ``X/10``。模拟盘不做红利税，统一税前口径。
- 送转 / 拆股：``每 10 股送转 Y 股`` -> ``ratio = 1 + Y/10``。
  数量乘 ratio、成本除 ratio，持仓市值不变（除权日开盘价会同步下调）。
- 配股：本轮**不实现**。配股要缴款、会同时改现金与成本，且需要用户决策，
  遇到就抛 StockError，避免静默算错。
- 历史 K 线：除权日**之前**的价格按 ratio 折算，成交量按 ratio 反向折算，
  保证走势连续、MA / 涨跌幅这些指标不失真。除权日及之后不动。
- 币市没有公司行为，调用方传进来会直接抛 StockError。
"""

import copy

from app.stockkit.errors import StockError
from app.stockkit.rules import CRYPTO_QTY_DECIMALS, is_crypto, lot_size

EPS = 1e-9

CASH_DIVIDEND = "dividend"       # 现金分红
SPLIT = "split"                  # 送转 / 拆股
SUSPEND = "suspend"              # 停牌（只影响撮合判定，不改持仓）
RESUME = "resume"                # 复牌

KINDS = (CASH_DIVIDEND, SPLIT, SUSPEND, RESUME)


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _round_qty(qty, symbol):
    """按标的最小单位取整：A 股向下取整到手，币市保留 6 位小数。"""
    qty = _f(qty)
    lot = lot_size(symbol)
    if lot <= 0:
        return round(qty, CRYPTO_QTY_DECIMALS)
    # 送转可能产生零股（A 股实际允许卖出零股），这里**不**向下取整到手，
    # 只抹掉浮点噪声，否则「10 送 3」的 130 股会被砍成 100 股。
    return int(round(qty))


def normalize_action(action, symbol=""):
    """校验并归一化一条公司行为，返回扁平 dict。

    接受 ``{"kind": "dividend", "per_10": 3.5}`` / ``{"kind":"split","per_10":3}``
    这类紧凑写法，也接受已经算好的 ``per_share`` / ``ratio``。
    """
    if not isinstance(action, dict):
        raise StockError("公司行为必须是字典")
    sym = str(symbol or action.get("symbol") or "").strip()
    if sym and is_crypto(sym):
        raise StockError("币市没有公司行为")
    kind = str(action.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise StockError("不支持的公司行为：%s" % (action.get("kind") or "空"))
    if kind == "配股" or str(action.get("kind")) == "rights":
        raise StockError("配股暂不支持（需要缴款，会同时改现金与成本）")

    out = {"kind": kind, "symbol": sym,
           "ex_date": str(action.get("ex_date") or "").strip()}

    if kind == CASH_DIVIDEND:
        if action.get("per_share") is not None:
            per_share = _f(action.get("per_share"))
        else:
            per_share = _f(action.get("per_10")) / 10.0
        if per_share < 0:
            raise StockError("每股分红不能为负")
        out["per_share"] = per_share
        out["ratio"] = 1.0
    elif kind == SPLIT:
        ratio = _f(action.get("ratio"))
        if ratio <= 0:
            ratio = 1.0 + _f(action.get("per_10")) / 10.0
        if ratio <= 0:
            raise StockError("送转比例必须大于 0")
        out["ratio"] = ratio
        out["per_share"] = 0.0
    else:                                  # 停牌 / 复牌：不改持仓
        out["ratio"] = 1.0
        out["per_share"] = 0.0
    return out


def split_position(qty, avg_cost, ratio):
    """送转 / 拆股后的 (数量, 成本价)。成本按比例摊薄，持仓成本额不变。"""
    qty = _f(qty)
    avg = _f(avg_cost)
    r = _f(ratio, 1.0)
    if r <= 0:
        raise StockError("送转比例必须大于 0")
    if qty <= 0:
        return 0.0, avg
    return qty * r, (avg / r if r else avg)


def dividend_cash(qty, per_share):
    """现金分红金额（元）。负数分红（补缴）按 0 处理。"""
    return max(0.0, _f(qty) * max(0.0, _f(per_share)))


def apply_to_position(qty, avg_cost, action):
    """把一条公司行为作用到单个持仓上。

    返回 ``{"qty", "avg_cost", "cash"}``：``cash`` 是该持仓应得的现金分红
    （由调用方加到玩家账户上），送转时恒为 0。
    """
    act = normalize_action(action)
    qty = _f(qty)
    avg = _f(avg_cost)
    if qty <= 0:
        return {"qty": 0.0, "avg_cost": avg, "cash": 0.0}

    if act["kind"] == CASH_DIVIDEND:
        return {"qty": qty, "avg_cost": avg,
                "cash": round(dividend_cash(qty, act["per_share"]), 2)}
    if act["kind"] == SPLIT:
        new_qty, new_avg = split_position(qty, avg, act["ratio"])
        return {"qty": round(new_qty, 6), "avg_cost": round(new_avg, 6), "cash": 0.0}
    return {"qty": qty, "avg_cost": avg, "cash": 0.0}


def adjust_history(bars, action):
    """把除权日**之前**的日 K 按比例折算，返回新列表（不改原对象）。

    分红：除权日前的价格减去每股分红（等价于比例折算的近似，保持连续）；
    送转：除权日前的 OHLC 与成交额除以 ratio，成交量乘 ratio。
    """
    act = normalize_action(action)
    ex_date = act["ex_date"]
    out = []
    for bar in bars or []:
        row = dict(bar) if isinstance(bar, dict) else {k: bar[k] for k in bar.keys()}
        if ex_date and str(row.get("date") or "") >= ex_date:
            out.append(row)
            continue
        if act["kind"] == SPLIT:
            r = act["ratio"]
            for key in ("open", "high", "low", "close"):
                if row.get(key) is not None:
                    row[key] = _f(row[key]) / r
            if row.get("volume") is not None:
                row["volume"] = _f(row["volume"]) * r
        elif act["kind"] == CASH_DIVIDEND:
            d = act["per_share"]
            for key in ("open", "high", "low", "close"):
                if row.get(key) is not None:
                    row[key] = max(0.0, _f(row[key]) - d)
        out.append(row)
    return out


def describe(action):
    """一句中文描述，给前端 / 日志用。"""
    act = normalize_action(action)
    date = ("（%s）" % act["ex_date"]) if act["ex_date"] else ""
    if act["kind"] == CASH_DIVIDEND:
        return "每 10 股派 %.2f 元%s" % (act["per_share"] * 10, date)
    if act["kind"] == SPLIT:
        return "每 10 股送转 %.2f 股，比例 %.4f%s" % (
            (act["ratio"] - 1.0) * 10, act["ratio"], date)
    if act["kind"] == SUSPEND:
        return "停牌%s" % date
    return "复牌%s" % date


def clone_bars(bars):
    """深拷贝一份日 K 序列（adjust_history 已返回新 dict，这里是便捷入口）。"""
    return [copy.deepcopy(b) for b in (bars or [])]
