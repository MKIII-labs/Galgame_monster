# -*- coding: utf-8 -*-
"""交易规则：代码规范化、手数、涨跌停、费用。

纯函数 + 常量，不碰数据库、不碰网络，可独立单测。
A 股 / ETF 与币市（Binance）的差异全部收敛在这里。
"""

import re

from app.stockkit.errors import StockError


BENCHMARK_SYMBOL = "sh000300"


BENCHMARK_NAME = "沪深300"


MAX_SYMBOLS = 12


MIN_SYMBOLS = 2


MAX_AI = 3


MAX_ORDERS_PER_DAY = 5


MAX_SINGLE_POSITION = 0.35          # 单只标的不超过总资产 35%


MARKET_CLOSE_MIN = 15 * 60 + 5      # 15:05 之后才认为当日日 K 已收定


DEFAULT_FEES = {
    "commission": 0.00025,          # 佣金 0.025%
    "min_commission": 5.0,          # 单笔最低 5 元
    "stamp_duty": 0.0005,           # 印花税 0.05%，仅卖出
    "transfer_fee": 0.00001,        # 过户费 0.001%，双边
    "slippage": 0.001,              # 模拟滑点 0.1%
}

# ---- 币市（Binance 公共行情，不需要 key）----


CRYPTO_SYMBOLS = {"BTCUSDT": "比特币", "ETHUSDT": "以太坊", "SOLUSDT": "Solana"}


CRYPTO_DEFAULT = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


CRYPTO_BENCHMARK = "BTCUSDT"


CRYPTO_BENCHMARK_NAME = "比特币"


CRYPTO_FEES = {
    "commission": 0.001,            # 单边 0.1%
    "min_commission": 0.0,
    "stamp_duty": 0.0,
    "transfer_fee": 0.0,
    "slippage": 0.0005,             # 0.05%
}


CRYPTO_QTY_DECIMALS = 6             # 币的最小交易单位：0.000001


_SYMBOL_RE = re.compile(r"^(sh|sz)(\d{6})$")


def is_crypto(symbol):
    """BTCUSDT / ETHUSDT / SOLUSDT。币市没有 sh/sz 前缀。"""
    return str(symbol or "").strip().upper() in CRYPTO_SYMBOLS


def normalize_symbol(raw):
    """把 600519 / sh600519 / SH600519 统一成 sh600519；币市统一成 BTCUSDT。"""
    s = str(raw or "").strip().lower().replace(" ", "")
    if not s:
        raise StockError("代码为空")
    if s.upper() in CRYPTO_SYMBOLS:
        return s.upper()
    m = _SYMBOL_RE.match(s)
    if m:
        return s
    if re.match(r"^\d{6}$", s):
        if s[0] in "65":            # 6xxxxx 沪市股票 / 5xxxxx 沪市 ETF
            return "sh" + s
        if s[0] in "0123":          # 0/3 深市股票，1 深市 ETF/LOF
            return "sz" + s
    raise StockError("无法识别的代码：%s" % raw)


def symbol_code(symbol):
    if is_crypto(symbol):
        return str(symbol).upper()[:-4]      # 去掉 USDT
    return normalize_symbol(symbol)[2:]


def is_etf(symbol):
    if is_crypto(symbol):
        return False
    code = symbol_code(symbol)
    return code.startswith(("51", "56", "58", "15", "16"))


def lot_size(symbol):
    """A 股与场内 ETF 都是 100 份一手；币市按 0.000001 递增，没有整手概念。"""
    return 0 if is_crypto(symbol) else 100


def limit_pct(symbol, name=""):
    """涨跌停幅度：ST 5%，科创/创业板 20%，其余 10%。ETF 按 10% 简化。币市无涨跌停。"""
    if is_crypto(symbol):
        return 0.0
    if "ST" in str(name or "").upper():
        return 0.05
    if symbol.startswith("sh688") or symbol.startswith("sz30"):
        return 0.20
    return 0.10


def calc_fee(side, amount, fees=None):
    """按成交额算费用（元）。买入不收印花税。"""
    f = dict(DEFAULT_FEES)
    f.update(fees or {})
    amount = max(0.0, float(amount or 0))
    fee = max(amount * float(f["commission"]), float(f["min_commission"]))
    fee += amount * float(f["transfer_fee"])
    if str(side) == "sell":
        fee += amount * float(f["stamp_duty"])
    return round(fee, 2)
