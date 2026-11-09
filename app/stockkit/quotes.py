# -*- coding: utf-8 -*-
"""行情读取：腾讯为主、东财日 K 兜底、Binance 币市。

只做「读数」：不撮合、不算账户；网络异常统一转 StockError。
缓存是进程内 TTL 缓存，`reset_store()` 会清空它。
"""

import datetime
import json
import math
import re
import threading
import time

import requests

from app.stockkit.errors import StockError
from app.stockkit.rules import (
    BENCHMARK_SYMBOL, CRYPTO_QTY_DECIMALS, CRYPTO_SYMBOLS, MARKET_CLOSE_MIN,
    _SYMBOL_RE, is_crypto, normalize_symbol, symbol_code,
)


TENCENT_QUOTE = "https://qt.gtimg.cn/q="


TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


TENCENT_SEARCH = "https://smartbox.gtimg.cn/s3/"


EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/24hr"


HTTP_TIMEOUT = (5, 15)


USER_AGENT = "Galgame_monster/1.0 (local stock simulation)"


_BARS_CACHE = {}


_BARS_TTL = 300


_QUOTE_CACHE = {}


_QUOTE_TTL = 5


_CACHE_LOCK = threading.Lock()
# 指标文本按「对局 + 模拟日」缓存：同一交易日的 3 个 AI 共用一次 K 线计算


def _http_get(url, params=None, timeout=HTTP_TIMEOUT, encoding=None):
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT},
                         timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise StockError("行情请求失败（%s）" % type(e).__name__)
    if r.status_code >= 400:
        raise StockError("行情接口返回 HTTP %s" % r.status_code)
    if encoding:
        return r.content.decode(encoding, "replace")
    return r


def _drop_partial_today(bars, now=None):
    """交易时段内最后一根日 K 是未收盘的临时 bar，结算前必须丢掉。"""
    if not bars:
        return bars
    now = now or datetime.datetime.now()
    today = now.date().isoformat()
    if bars[-1].get("date") != today:
        return bars
    if now.hour * 60 + now.minute < MARKET_CLOSE_MIN:
        return bars[:-1]
    return bars


def _fnum(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _qnum(v):
    """数量输出：整数股保持 int，币市小数保留 6 位。"""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return 0
    if abs(f - round(f)) < 1e-9:
        return int(round(f))
    return round(f, CRYPTO_QTY_DECIMALS)


def _bar_row(date, open_, close, high, low, volume, amount=0.0):
    return {"date": str(date)[:10], "open": _fnum(open_), "close": _fnum(close),
            "high": _fnum(high), "low": _fnum(low), "volume": _fnum(volume),
            "amount": _fnum(amount)}


def fetch_daily(symbol, count=320, use_cache=True):
    """取日 K（前复权）。腾讯失败自动退到东财。返回按日期升序的 bar 列表。"""
    symbol = normalize_symbol(symbol)
    count = max(20, min(int(count or 320), 1000))
    key = (symbol, count)
    now = time.time()
    if use_cache:
        with _CACHE_LOCK:
            hit = _BARS_CACHE.get(key)
            if hit and now - hit[0] < _BARS_TTL:
                return [dict(x) for x in hit[1]]
    bars = []
    if is_crypto(symbol):
        bars = _fetch_daily_binance(symbol, count)
    else:
        try:
            bars = _fetch_daily_tencent(symbol, count)
        except StockError:
            bars = []
        if not bars:
            bars = _fetch_daily_eastmoney(symbol, count)
    if not bars:
        raise StockError("拿不到 %s 的历史行情" % symbol)
    bars.sort(key=lambda x: x["date"])
    if use_cache:
        with _CACHE_LOCK:
            _BARS_CACHE[key] = (now, [dict(x) for x in bars])
    return bars


def _fetch_daily_tencent(symbol, count):
    # 指数没有复权概念；股票 / ETF 用前复权，避免除权缺口造成假暴跌。
    # 注意不能用「代码以 000 开头」判断指数：sz000001 是平安银行，不是指数。
    code = symbol_code(symbol)
    is_index = symbol.startswith("sh000") or code.startswith("399")
    qfq = "" if is_index else "qfq"
    url = "%s?param=%s,day,,,%d,%s" % (TENCENT_KLINE, symbol, count, qfq)
    data = _http_get(url).json()
    node = ((data or {}).get("data") or {}).get(symbol) or {}
    arr = node.get("qfqday") or node.get("day") or []
    out = []
    for row in arr:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        out.append(_bar_row(row[0], row[1], row[2], row[3], row[4], row[5],
                            row[6] if len(row) > 6 else 0))
    return out


def _fetch_daily_eastmoney(symbol, count):
    market = 1 if symbol.startswith("sh") else 0
    secid = "%d.%s" % (market, symbol_code(symbol))
    params = {
        "secid": secid, "klt": 101, "fqt": 1, "beg": 19900101, "end": 20500101,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    data = _http_get(EASTMONEY_KLINE, params=params).json()
    klines = ((data or {}).get("data") or {}).get("klines") or []
    out = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        out.append(_bar_row(parts[0], parts[1], parts[2], parts[3], parts[4],
                            parts[5], parts[6] if len(parts) > 6 else 0))
    return out[-count:]


def _fetch_daily_binance(symbol, count):
    """Binance 日线。返回按日期升序；丢弃尚未收盘的当日 K。"""
    limit = max(20, min(int(count or 320), 1000))
    data = _http_get(BINANCE_KLINES, params={
        "symbol": symbol, "interval": "1d", "limit": limit}).json()
    if not isinstance(data, list):
        raise StockError("拿不到 %s 的币市行情" % symbol)
    now_ms = int(time.time() * 1000)
    out = []
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        try:
            if int(row[6]) > now_ms:        # close_time 还没到 → 当日 K 未收定
                continue
            date = datetime.datetime.fromtimestamp(
                int(row[0]) / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            continue
        out.append(_bar_row(date, _fnum(row[1]), _fnum(row[4]), _fnum(row[2]),
                            _fnum(row[3]), _fnum(row[5]),
                            _fnum(row[7]) if len(row) > 7 else 0))
    return out[-limit:]


def fetch_benchmark(count=320, use_cache=True, symbol=BENCHMARK_SYMBOL):
    """基准日 K：A 股用沪深300，币市用 BTC。同时充当交易日历。"""
    return fetch_daily(symbol, count, use_cache=use_cache)


def fetch_quotes(symbols):
    """批量取实时/延时报价。返回 {symbol: {...}}；拿不到的标的不出现在结果里。"""
    syms = [normalize_symbol(s) for s in (symbols or [])]
    if not syms:
        return {}
    now = time.time()
    with _CACHE_LOCK:
        cached = {s: _QUOTE_CACHE[s][1] for s in syms
                  if s in _QUOTE_CACHE and now - _QUOTE_CACHE[s][0] < _QUOTE_TTL}
    missing = [s for s in syms if s not in cached]
    if missing:
        stocks = [s for s in missing if not is_crypto(s)]
        coins = [s for s in missing if is_crypto(s)]
        if stocks:
            text = _http_get(TENCENT_QUOTE + ",".join(stocks), encoding="gbk")
            for line in text.splitlines():
                m = re.search(r'v_((?:sh|sz)\d{6})="(.*)";', line)
                if not m:
                    continue
                symbol, payload = m.group(1), m.group(2)
                p = payload.split("~")
                if len(p) < 6:
                    continue
                item = {
                    "symbol": symbol,
                    "code": p[2] if len(p) > 2 else symbol[2:],
                    "name": p[1] if len(p) > 1 else symbol,
                    "price": _fnum(p[3]),
                    "prev_close": _fnum(p[4]),
                    "open": _fnum(p[5]),
                    "volume": _fnum(p[6]) if len(p) > 6 else 0,
                    "time": p[30] if len(p) > 30 else "",
                    "change": _fnum(p[31]) if len(p) > 31 else 0,
                    "change_pct": _fnum(p[32]) if len(p) > 32 else 0,
                    "high": _fnum(p[33]) if len(p) > 33 else 0,
                    "low": _fnum(p[34]) if len(p) > 34 else 0,
                }
                cached[symbol] = item
                with _CACHE_LOCK:
                    _QUOTE_CACHE[symbol] = (now, dict(item))
        for symbol in coins:
            try:
                d = _http_get(BINANCE_TICKER, params={"symbol": symbol}).json() or {}
            except StockError:
                continue
            if not isinstance(d, dict) or not d.get("lastPrice"):
                continue
            item = {
                "symbol": symbol, "code": symbol_code(symbol),
                "name": CRYPTO_SYMBOLS.get(symbol, symbol),
                "price": _fnum(d.get("lastPrice")),
                "prev_close": _fnum(d.get("prevClosePrice")),
                "open": _fnum(d.get("openPrice")),
                "volume": _fnum(d.get("volume")),
                "time": "", "change": _fnum(d.get("priceChange")),
                "change_pct": _fnum(d.get("priceChangePercent")),
                "high": _fnum(d.get("highPrice")),
                "low": _fnum(d.get("lowPrice")),
            }
            cached[symbol] = item
            with _CACHE_LOCK:
                _QUOTE_CACHE[symbol] = (now, dict(item))
    return {s: cached[s] for s in syms if s in cached}


def search_symbols(keyword, limit=8):
    """中文名 / 拼音 / 代码搜索。币市走固定名单，A 股走腾讯联想。"""
    q = str(keyword or "").strip()
    if not q:
        return []
    up = q.upper()
    coins = []
    for sym, nm in CRYPTO_SYMBOLS.items():
        base = sym[:-4]
        if up in (sym, base) or q in nm:
            coins.append({"symbol": sym, "code": base, "name": nm, "type": "币"})
    if coins:
        return coins[:max(1, int(limit or 8))]
    text = _http_get(TENCENT_SEARCH, params={"q": q, "t": "all"}, encoding="utf-8")
    m = re.search(r'v_hint="(.*)"', text)
    if not m:
        return []
    try:
        payload = json.loads('"' + m.group(1).replace('"', '\\"') + '"')
    except Exception:
        payload = m.group(1)
    out = []
    for item in payload.split("^"):
        parts = item.split("~")
        if len(parts) < 5:
            continue
        market, code, name, _pinyin, typ = parts[:5]
        if market not in ("sh", "sz") or typ not in ("GP-A", "ETF"):
            continue
        symbol = market + code
        out.append({"symbol": symbol, "code": code, "name": name,
                    "type": "ETF" if typ == "ETF" else "股票"})
        if len(out) >= max(1, int(limit or 8)):
            break
    return out
