# -*- coding: utf-8 -*-
"""撮合内核：市价 / 限价、部分成交、涨跌停与停牌判定。

设计原则：**只做判定，不碰数据库**。

`_execute_pending`（在 app/stock.py 里）负责把当日 bar、持仓、现金预取进内存，
然后逐笔调用本模块的 `plan_fill()` 拿一个「成交计划」，再落库。
好处是撮合规则可以脱离 SQLite 单测：给定一根日 K 与一笔委托，
成交价 / 成交量 / 不成交原因都是纯函数输出。

日 K 撮合的固有限制（写明，避免以后误以为能精确复现盘口）：

- 一根日 K 只有 OHLC + 成交量，没有盘口与逐笔，所以**无法**知道限价单当天
  是否真的被触及过。本模块的近似是：
    市价单  -> 以开盘价 ± 滑点成交，价格夹在当日 [low, high] 内；
    限价买  -> 开盘价 <= 限价时成交（价格取 min(开盘价+滑点, 限价)）；
               开盘价 > 限价但 low <= 限价时，视为盘中触及，按限价成交；
               否则不成交，委托继续挂着（可跨日）。
- 部分成交按「参与率」近似：单日最多吃掉当日成交量的 `PARTICIPATION`。
  A 股没有官方「散户参与率」口径，这里取 10% 作为量级假设，
  目的是让大单不能在一根日 K 里无摩擦全部成交，而不是声称精确。
- 停牌（volume<=0 或 open<=0）时，市价单直接判废；限价单保留挂单，
  由调用方按 `MAX_PENDING_DAYS` 做超时撤单，避免永久占着冻结资金。
"""
import math

from app.stockkit.rules import lot_size

PARTICIPATION = 0.10        # 单日最多吃掉当日成交量的比例（近似值，见模块注释）
MAX_PENDING_DAYS = 5        # 挂单最多存活几个模拟交易日
EPS = 1e-9

BUY, SELL = "buy", "sell"


class Fill:
    """一笔撮合的结果。

    qty=0 表示不成交；`reason` 非空时是**判废**原因（委托直接变成 rejected），
    为空则委托继续 pending。这样调用方不需要自己判断「该挂还是该废」。
    """

    __slots__ = ("price", "qty", "fee_base", "reason", "partial")

    def __init__(self, price=0.0, qty=0.0, reason=""):
        self.price = float(price)
        self.qty = float(qty)
        self.reason = reason or ""
        self.fee_base = self.price * self.qty     # 计费基数 = 成交额
        self.partial = False

    @property
    def filled(self):
        return self.qty > EPS and not self.reason

    def __repr__(self):
        return "Fill(price=%s, qty=%s, reason=%r)" % (self.price, self.qty, self.reason)


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def is_suspended(bar):
    """当日是否停牌（无行情 / 零成交 / 无开盘价）。"""
    if not bar:
        return True
    return _f(bar["volume"]) <= 0 or _f(bar["open"]) <= 0


def cap_by_volume(qty, symbol, volume):
    """按参与率把成交量限制在当日成交量的一定比例内，并向下取整到交易单位。"""
    cap = _f(volume) * PARTICIPATION
    if cap <= 0:
        return 0.0
    if cap < qty:
        qty = cap
    lot = lot_size(symbol)
    if lot <= 0:                      # 币市：按 1e-6 向下取整
        return math.floor(qty * 1e6) / 1e6
    return float(int(qty // lot) * lot)


def limit_up_down(prev_close, limit):
    """返回 (涨停价, 跌停价)。limit<=0（币市）返回 (0, 0) 表示不限制。"""
    pc = _f(prev_close)
    if pc <= 0 or _f(limit) <= 0:
        return 0.0, 0.0
    return pc * (1 + limit), pc * (1 - limit)


def blocked_by_limit(side, open_price, prev_close, limit):
    """开盘即涨跌停时能否成交。返回判废原因或空串。"""
    up, down = limit_up_down(prev_close, limit)
    if up <= 0:
        return ""
    px = _f(open_price)
    if side == BUY and px >= up - 1e-6:
        return "开盘涨停，买不进"
    if side == SELL and px <= down + 1e-6:
        return "开盘跌停，卖不出"
    return ""


def _clamp(px, bar):
    low = _f(bar["low"]) or _f(bar["open"])
    high = _f(bar["high"]) or _f(bar["open"])
    if low > high:
        low, high = high, low
    return min(max(px, low), high)


def plan_fill(side, qty, bar, prev_close=0.0, limit=0.0, limit_price=0.0,
              slippage=0.0, pending_days=0, symbol=""):
    """核心：给定一笔委托与一根日 K，算出成交计划。

    参数：
      side        buy / sell
      qty         未成交数量（剩余）
      bar         当日日 K（dict / sqlite3.Row，含 open/high/low/close/volume）
      prev_close  前收盘价（算涨跌停用）
      limit       涨跌停幅度（0 = 不限制，币市）
      limit_price 限价（0 = 市价单）
      slippage    滑点（小数）
      pending_days 已经挂了几个交易日（用于超时判废）

    返回 Fill。qty=0 且 reason="" 表示「今天没轮到成交，明天继续」。
    """
    qty = _f(qty)
    if qty <= 0:
        return Fill(reason="委托数量非法")

    if is_suspended(bar):
        if _f(limit_price) > 0 and pending_days < MAX_PENDING_DAYS:
            return Fill()                     # 限价单在停牌期间保留
        return Fill(reason="停牌或没有当日行情")

    if pending_days >= MAX_PENDING_DAYS:
        return Fill(reason="挂单超过 %d 个交易日未成交，已自动撤销" % MAX_PENDING_DAYS)

    op = _f(bar["open"])
    block = blocked_by_limit(side, op, prev_close, limit)
    if block:
        if _f(limit_price) > 0 and pending_days + 1 < MAX_PENDING_DAYS:
            return Fill()                     # 涨跌停封板：限价单继续排队
        return Fill(reason=block)

    lp = _f(limit_price)
    if lp <= 0:                               # 市价单：开盘价 ± 滑点
        px = op * (1 + slippage) if side == BUY else op * (1 - slippage)
        px = _clamp(px, bar)
    else:
        low = _f(bar["low"]) or op
        high = _f(bar["high"]) or op
        if side == BUY:
            if op <= lp:                       # 开盘就在限价内：按更优的开盘价成交
                px = min(op * (1 + slippage), lp)
            elif low <= lp:                    # 盘中回落到限价
                px = lp
            else:
                return Fill()                  # 没到价，明天继续
        else:
            if op >= lp:
                px = max(op * (1 - slippage), lp)
            elif high >= lp:
                px = lp
            else:
                return Fill()

    px = _clamp(px, bar)
    # symbol 决定取整单位：A 股 / ETF 按 100 股整手，币市按 1e-6。
    # 早先这里写死空串，币市会被当成 A 股按 100 取整 —— 0.5 BTC 直接成交不了。
    filled = cap_by_volume(qty, symbol, float(bar["volume"]))
    if filled <= EPS:
        return Fill(reason="当日成交量不足，无法成交")
    plan = Fill(price=px, qty=min(qty, filled))
    plan.partial = plan.qty + EPS < qty
    return plan


def fill_qty(qty, symbol, volume):
    """对外暴露的成交量取整（按手 / 按 1e-6），供调用方在 plan_fill 之后再夹一次。"""
    return cap_by_volume(qty, symbol, volume)
