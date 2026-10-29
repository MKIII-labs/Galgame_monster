# -*- coding: utf-8 -*-
"""功能大厅：模拟股市（真实 A 股 / ETF 行情 + AI 对手）。

设计要点：
- 行情只做「读数」：腾讯行情为主，东财日 K 兜底；不接券商、不下真实订单。
- 一局模拟盘把行情快照写进本地 SQLite，之后按模拟日推进；
  AI 与人类都只能看到 `date <= cursor_date` 的数据，从机制上杜绝未来函数。
- 成交规则按 A 股简化：T+1、100 股整手、涨跌停、佣金/印花税/过户费、滑点。
- AI 只输出 JSON 订单，服务端二次校验后才进撮合队列。

所有函数都不抛网络库异常，统一转成 StockError（带可读中文原因）。
"""
import datetime
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import uuid

import requests

from app import llm
from app.config import get_api_conf
from app.store import DATA_DIR

STOCK_DB = os.path.join(DATA_DIR, "stock.db")

TENCENT_QUOTE = "https://qt.gtimg.cn/q="
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_SEARCH = "https://smartbox.gtimg.cn/s3/"
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

BENCHMARK_SYMBOL = "sh000300"
BENCHMARK_NAME = "沪深300"

MAX_SYMBOLS = 12
MIN_SYMBOLS = 2
MAX_AI = 3
MAX_ORDERS_PER_DAY = 5
MAX_SINGLE_POSITION = 0.35          # 单只标的不超过总资产 35%
MARKET_CLOSE_MIN = 15 * 60 + 5      # 15:05 之后才认为当日日 K 已收定
HTTP_TIMEOUT = (5, 15)
USER_AGENT = "Galgame_monster/1.0 (local stock simulation)"

DEFAULT_FEES = {
    "commission": 0.00025,          # 佣金 0.025%
    "min_commission": 5.0,          # 单笔最低 5 元
    "stamp_duty": 0.0005,           # 印花税 0.05%，仅卖出
    "transfer_fee": 0.00001,        # 过户费 0.001%，双边
    "slippage": 0.001,              # 模拟滑点 0.1%
}

# ---- 币市（Binance 公共行情，不需要 key）----
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/24hr"
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

PERSONAS = [
    {"id": "ai_value", "name": "价值派老陈",
     "style": "偏好估值合理、盈利稳定、现金流好的公司；愿意长期持有，换手低，"
              "对短期波动不敏感，但对基本面恶化会果断减仓。"},
    {"id": "ai_trend", "name": "趋势派小夏",
     "style": "顺势而为；关注均线多头排列、放量突破和趋势延续，趋势破坏就止损，"
              "不抄底、不逆势加仓。"},
    {"id": "ai_contrarian", "name": "逆势派阿远",
     "style": "寻找超跌和均值回归机会；分批买入、反弹卖出，严格控制单票仓位，"
              "宁可错过也不追高。"},
]


class StockError(Exception):
    """可读的业务错误（行情失败、订单非法、对局不存在等）。"""


# ============================================================
# 行情：代码、搜索、报价、日 K
# ============================================================

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


_BARS_CACHE = {}
_BARS_TTL = 300
_QUOTE_CACHE = {}
_QUOTE_TTL = 5
_CACHE_LOCK = threading.Lock()
# 指标文本按「对局 + 模拟日」缓存：同一交易日的 3 个 AI 共用一次 K 线计算
_IND_CACHE = {}
_IND_CACHE_MAX = 16


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


# ============================================================
# SQLite
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL DEFAULT '',
  mode          TEXT NOT NULL DEFAULT 'replay',
  market        TEXT NOT NULL DEFAULT 'stock',
  status        TEXT NOT NULL DEFAULT 'running',
  created_at    REAL NOT NULL,
  start_date    TEXT NOT NULL,
  end_date      TEXT NOT NULL DEFAULT '',
  cursor_date   TEXT NOT NULL,
  initial_cash  REAL NOT NULL,
  commission    REAL NOT NULL,
  min_commission REAL NOT NULL,
  stamp_duty    REAL NOT NULL,
  transfer_fee  REAL NOT NULL,
  slippage      REAL NOT NULL,
  benchmark     TEXT NOT NULL DEFAULT 'sh000300'
);

CREATE TABLE IF NOT EXISTS players (
  id            TEXT NOT NULL,
  game_id       TEXT NOT NULL,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL,
  persona       TEXT NOT NULL DEFAULT '',
  cash          REAL NOT NULL,
  initial_cash  REAL NOT NULL,
  realized_pnl  REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (game_id, id)
);
CREATE INDEX IF NOT EXISTS idx_stock_players_game ON players(game_id);

CREATE TABLE IF NOT EXISTS symbols (
  game_id       TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'stock',
  tradable      INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (game_id, symbol)
);

CREATE TABLE IF NOT EXISTS bars (
  game_id       TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  date          TEXT NOT NULL,
  open          REAL NOT NULL,
  high          REAL NOT NULL,
  low           REAL NOT NULL,
  close         REAL NOT NULL,
  volume        REAL NOT NULL DEFAULT 0,
  amount        REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (game_id, symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_stock_bars_date ON bars(game_id, date);

CREATE TABLE IF NOT EXISTS orders (
  id            TEXT PRIMARY KEY,
  game_id       TEXT NOT NULL,
  player_id     TEXT NOT NULL,
  created_date  TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  side          TEXT NOT NULL,
  qty           INTEGER NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',
  reason        TEXT NOT NULL DEFAULT '',
  reject        TEXT NOT NULL DEFAULT '',
  fill_date     TEXT NOT NULL DEFAULT '',
  fill_price    REAL NOT NULL DEFAULT 0,
  fee           REAL NOT NULL DEFAULT 0,
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_orders_game ON orders(game_id, status);

CREATE TABLE IF NOT EXISTS positions (
  game_id       TEXT NOT NULL,
  player_id     TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  qty           INTEGER NOT NULL DEFAULT 0,
  available_qty INTEGER NOT NULL DEFAULT 0,
  avg_cost      REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (game_id, player_id, symbol)
);

CREATE TABLE IF NOT EXISTS trades (
  id            TEXT PRIMARY KEY,
  game_id       TEXT NOT NULL,
  player_id     TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  side          TEXT NOT NULL,
  date          TEXT NOT NULL,
  qty           INTEGER NOT NULL,
  price         REAL NOT NULL,
  fee           REAL NOT NULL,
  amount        REAL NOT NULL,
  realized_pnl  REAL NOT NULL DEFAULT 0,
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_trades_game ON trades(game_id, date);

CREATE TABLE IF NOT EXISTS snapshots (
  game_id       TEXT NOT NULL,
  player_id     TEXT NOT NULL,
  date          TEXT NOT NULL,
  cash          REAL NOT NULL,
  market_value  REAL NOT NULL,
  equity        REAL NOT NULL,
  pnl           REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (game_id, player_id, date)
);

CREATE TABLE IF NOT EXISTS journals (
  game_id       TEXT NOT NULL,
  player_id     TEXT NOT NULL,
  date          TEXT NOT NULL,
  text          TEXT NOT NULL DEFAULT '',
  created_at    REAL NOT NULL,
  PRIMARY KEY (game_id, player_id, date)
);

-- AI 分析记录：journals 只有一句复盘，这里存可检索的结构化分析
CREATE TABLE IF NOT EXISTS analyses (
  id            TEXT PRIMARY KEY,
  game_id       TEXT NOT NULL,
  player_id     TEXT NOT NULL,
  date          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT '复盘',
  text          TEXT NOT NULL DEFAULT '',
  keywords      TEXT NOT NULL DEFAULT '',
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_analyses_date ON analyses(game_id, date);
CREATE INDEX IF NOT EXISTS idx_stock_analyses_kind ON analyses(game_id, kind, date);

-- 分析 ↔ 标的关联：按标的筛选走索引，不去 LIKE 扫 text
CREATE TABLE IF NOT EXISTS analysis_symbols (
  game_id       TEXT NOT NULL,
  analysis_id   TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  PRIMARY KEY (analysis_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_stock_ansym ON analysis_symbols(game_id, symbol);

-- 冷数据：已结束对局的旧分析从热表搬到这里，检索时 UNION ALL
CREATE TABLE IF NOT EXISTS analyses_cold (
  id            TEXT PRIMARY KEY,
  game_id       TEXT NOT NULL,
  player_id     TEXT NOT NULL,
  date          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT '复盘',
  text          TEXT NOT NULL DEFAULT '',
  keywords      TEXT NOT NULL DEFAULT '',
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_analyses_cold_date ON analyses_cold(game_id, date);
CREATE INDEX IF NOT EXISTS idx_stock_analyses_cold_kind ON analyses_cold(game_id, kind, date);
CREATE TABLE IF NOT EXISTS analysis_symbols_cold (
  game_id       TEXT NOT NULL,
  analysis_id   TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  PRIMARY KEY (analysis_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_stock_ansym_cold ON analysis_symbols_cold(game_id, symbol);

-- 补索引：原表只有 (game_id, status) / (game_id, date)，按玩家查时用不上
CREATE INDEX IF NOT EXISTS idx_stock_orders_player
  ON orders(game_id, player_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_stock_trades_player
  ON trades(game_id, player_id, created_at);

-- 盲预测训练：每个模拟日对 1/3/5 日后的方向下注，到期自动结算
CREATE TABLE IF NOT EXISTS predictions (
  id            TEXT NOT NULL UNIQUE,
  game_id       TEXT NOT NULL,
  player_id     TEXT NOT NULL DEFAULT 'you',
  created_date  TEXT NOT NULL,
  target        TEXT NOT NULL,              -- 标的代码，或 'benchmark'（沪深300）
  horizon       INTEGER NOT NULL,           -- 1 / 3 / 5 个交易日
  direction     TEXT NOT NULL,              -- up / down / flat
  base_close    REAL NOT NULL,
  threshold     REAL NOT NULL,              -- 自适应阈值（百分数，创建时锁定）
  due_date      TEXT NOT NULL DEFAULT '',
  settle_close  REAL NOT NULL DEFAULT 0,
  actual_pct    REAL NOT NULL DEFAULT 0,
  hit           INTEGER NOT NULL DEFAULT -1, -- -1 未结算 / 0 错 / 1 对
  created_at    REAL NOT NULL,
  settled_at    REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (game_id, player_id, target, created_date, horizon)
);
CREATE INDEX IF NOT EXISTS idx_stock_predictions_date
  ON predictions(game_id, player_id, created_date);
CREATE INDEX IF NOT EXISTS idx_stock_predictions_hit
  ON predictions(game_id, player_id, hit, created_date);
"""

_local = threading.local()
_init_lock = threading.Lock()
_inited = False
_GAME_LOCK = threading.RLock()
_AI_BUSY = set()

_FTS_MODE = ""            # "" = FTS 不可用（检索退化为 LIKE）；"trigram"
_FTS_CAPABLE = None       # None = 还没探测过当前 SQLite 是否支持 trigram
_FTS_MIN_LEN = 3          # trigram 分词至少要 3 个字符才 MATCH，短词一律走 LIKE


def _setup_fts(conn):
    """探测 FTS5 + trigram 分词，并在当前库里建好索引表（每个库都要建）。

    只有 trigram 才对中文子串检索有效：unicode61 会把一整句中文当成一个 token，
    搜「突破」根本命中不了。探测不到 trigram 就不建索引，检索退化到带索引预筛的 LIKE。
    analyses 是外部内容表（content='analyses'），按 FTS5 官方做法直接向索引表写入
    (rowid, text, keywords)，不用触发器；批量搬库后再整体 rebuild。
    """
    global _FTS_MODE, _FTS_CAPABLE
    if _FTS_CAPABLE is None:
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe "
                         "USING fts5(x, tokenize='trigram')")
            conn.execute("DROP TABLE _fts_probe")
            _FTS_CAPABLE = True
        except sqlite3.Error:
            _FTS_CAPABLE = False
    if not _FTS_CAPABLE:
        _FTS_MODE = ""
        return ""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS analyses_fts USING fts5("
            "text, keywords, content='analyses', content_rowid='rowid', "
            "tokenize='trigram')")
    except sqlite3.Error:
        _FTS_MODE = ""
        return ""
    _FTS_MODE = "trigram"
    return _FTS_MODE


def _fts_insert(conn, rowid, text, keywords):
    if not _FTS_MODE:
        return
    conn.execute("INSERT INTO analyses_fts(rowid, text, keywords) VALUES(?,?,?)",
                 (rowid, text or "", keywords or ""))


def rebuild_analyses_fts(conn=None):
    """整体重建热表分析索引。批量写库 / 清理之后调用一次即可。"""
    conn = conn or get_db()
    if not _FTS_MODE:
        return False
    conn.execute("INSERT INTO analyses_fts(analyses_fts) VALUES('rebuild')")
    return True


def fts_mode():
    """当前检索实现（"" = LIKE 兜底），测试 / 自检用。"""
    return _FTS_MODE


def get_db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        parent = os.path.dirname(STOCK_DB)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(STOCK_DB, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


_PLAYERS_DDL = """
CREATE TABLE players (
  id            TEXT NOT NULL,
  game_id       TEXT NOT NULL,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL,
  persona       TEXT NOT NULL DEFAULT '',
  cash          REAL NOT NULL,
  initial_cash  REAL NOT NULL,
  realized_pnl  REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (game_id, id)
)
"""


def _ensure_player_pk(conn):
    """把旧库的 players 主键从 (id) 迁到 (game_id, id)。

    早期 schema 用了 `id TEXT PRIMARY KEY`，而玩家 id 是固定的 `you` / `ai_value`，
    于是第二局开局就会撞 UNIQUE 约束 —— 一局之后再开不了新局。
    SQLite 不能改主键，只能重建表再搬数据。表间没有外键引用 players，重建是安全的。
    """
    info = conn.execute("PRAGMA table_info(players)").fetchall()
    if not info:
        return
    pk = sorted(r["name"] for r in info if r["pk"])
    if pk == ["game_id", "id"]:
        return
    conn.executescript(
        "ALTER TABLE players RENAME TO players_old;\n"
        + _PLAYERS_DDL + ";\n"
        "INSERT OR IGNORE INTO players(id, game_id, name, kind, persona, cash, "
        "initial_cash, realized_pnl) SELECT id, game_id, name, kind, persona, cash, "
        "initial_cash, realized_pnl FROM players_old;\n"
        "DROP TABLE players_old;\n"
        "CREATE INDEX IF NOT EXISTS idx_stock_players_game ON players(game_id);\n"
    )
    print("[模拟股市] players 主键已迁移为 (game_id, id)")


def _ensure_game_market(conn):
    """旧库没有 market 列：补一列，历史对局默认按 A 股处理。"""
    info = conn.execute("PRAGMA table_info(games)").fetchall()
    if not info or any(r["name"] == "market" for r in info):
        return
    conn.execute("ALTER TABLE games ADD COLUMN market TEXT NOT NULL DEFAULT 'stock'")
    print("[模拟股市] games 已补充 market 列")


def init_db():
    global _inited
    with _init_lock:
        if _inited:
            return
        conn = get_db()
        conn.executescript(SCHEMA)
        _ensure_player_pk(conn)
        _ensure_game_market(conn)
        _setup_fts(conn)
        conn.commit()
        _inited = True


def configure(path):
    """测试用：把数据库指向另一个文件。"""
    global STOCK_DB, _inited
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
    STOCK_DB = os.path.abspath(path)
    _inited = False
    init_db()


def reset_store():
    """清空所有对局（测试 / 调试）。"""
    init_db()
    with _GAME_LOCK:
        conn = get_db()
        for table in ("games", "players", "symbols", "bars", "orders",
                      "positions", "trades", "snapshots", "journals",
                      "analyses", "analysis_symbols",
                      "analyses_cold", "analysis_symbols_cold", "predictions"):
            conn.execute("DELETE FROM " + table)
        conn.commit()
        rebuild_analyses_fts(conn)
    with _CACHE_LOCK:
        _BARS_CACHE.clear()
        _QUOTE_CACHE.clear()


def _row(row):
    return dict(row) if row else None


def _game(conn, game_id):
    g = _row(conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone())
    if not g:
        raise StockError("对局不存在")
    return g


def _player(conn, game_id, player_id):
    p = _row(conn.execute(
        "SELECT * FROM players WHERE game_id=? AND id=?",
        (game_id, player_id)).fetchone())
    if not p:
        raise StockError("对局里没有这个参与者")
    return p


def _symbols(conn, game_id, tradable_only=True):
    sql = "SELECT * FROM symbols WHERE game_id=?"
    if tradable_only:
        sql += " AND tradable=1"
    sql += " ORDER BY rowid"
    return [_row(r) for r in conn.execute(sql, (game_id,)).fetchall()]


def _is_tradable(conn, game_id, symbol):
    r = conn.execute(
        "SELECT 1 FROM symbols WHERE game_id=? AND symbol=? AND tradable=1",
        (game_id, symbol)).fetchone()
    return bool(r)


def _position(conn, game_id, player_id, symbol):
    return _row(conn.execute(
        "SELECT * FROM positions WHERE game_id=? AND player_id=? AND symbol=?",
        (game_id, player_id, symbol)).fetchone())


def _positions(conn, game_id, player_id):
    return [_row(r) for r in conn.execute(
        "SELECT * FROM positions WHERE game_id=? AND player_id=? AND qty>0",
        (game_id, player_id)).fetchall()]


def _positions_with_last(conn, game_id, player_id, date):
    """持仓 + 该日之前最后一根收盘价，一条 SQL 拿齐（原来是一只标的一次查询）。"""
    return [_row(r) for r in conn.execute(
        "SELECT p.symbol, p.qty, p.available_qty, p.avg_cost, "
        "(SELECT b.close FROM bars b WHERE b.game_id=p.game_id AND b.symbol=p.symbol "
        " AND b.date<=? ORDER BY b.date DESC LIMIT 1) AS last "
        "FROM positions p WHERE p.game_id=? AND p.player_id=? AND p.qty>0",
        (date, game_id, player_id)).fetchall()]


def _bar(conn, game_id, symbol, date):
    return _row(conn.execute(
        "SELECT * FROM bars WHERE game_id=? AND symbol=? AND date=?",
        (game_id, symbol, date)).fetchone())


def _last_bar(conn, game_id, symbol, date, strict=False):
    op = "<" if strict else "<="
    return _row(conn.execute(
        "SELECT * FROM bars WHERE game_id=? AND symbol=? AND date" + op + "? "
        "ORDER BY date DESC LIMIT 1", (game_id, symbol, date)).fetchone())


def _last_close(conn, game_id, symbol, date):
    b = _last_bar(conn, game_id, symbol, date)
    return float(b["close"]) if b else 0.0


def _snapshot(conn, game_id, player, date):
    cash = float(player["cash"])
    mv = 0.0
    for pos in _positions_with_last(conn, game_id, player["id"], date):
        mv += float(pos["qty"]) * float(pos["last"] or 0)
    equity = cash + mv
    pnl = equity - float(player["initial_cash"])
    conn.execute(
        "INSERT OR REPLACE INTO snapshots"
        "(game_id,player_id,date,cash,market_value,equity,pnl) VALUES(?,?,?,?,?,?,?)",
        (game_id, player["id"], date, cash, mv, equity, pnl))
    return {"cash": cash, "market_value": mv, "equity": equity, "pnl": pnl}


def _snapshot_all(conn, game_id, date):
    """一次事务里给全部参与者估值：固定 2 条 SQL，跟人数无关。"""
    players = [_row(r) for r in conn.execute(
        "SELECT * FROM players WHERE game_id=?", (game_id,)).fetchall()]
    mv_by_player = {}
    for r in conn.execute(
            "SELECT p.player_id AS pid, p.qty, "
            "(SELECT b.close FROM bars b WHERE b.game_id=p.game_id AND b.symbol=p.symbol "
            " AND b.date<=? ORDER BY b.date DESC LIMIT 1) AS last "
            "FROM positions p WHERE p.game_id=? AND p.qty>0", (date, game_id)).fetchall():
        mv_by_player[r["pid"]] = mv_by_player.get(r["pid"], 0.0) \
            + float(r["qty"]) * float(r["last"] or 0)
    rows = []
    for p in players:
        cash = float(p["cash"])
        mv = mv_by_player.get(p["id"], 0.0)
        equity = cash + mv
        rows.append((game_id, p["id"], date, cash, mv, equity,
                     equity - float(p["initial_cash"])))
    conn.executemany(
        "INSERT OR REPLACE INTO snapshots"
        "(game_id,player_id,date,cash,market_value,equity,pnl) VALUES(?,?,?,?,?,?,?)", rows)


def _journal_exists(conn, game_id, player_id, date):
    return bool(conn.execute(
        "SELECT 1 FROM journals WHERE game_id=? AND player_id=? AND date=?",
        (game_id, player_id, date)).fetchone())


def _orders_exist(conn, game_id, player_id, date):
    return bool(conn.execute(
        "SELECT 1 FROM orders WHERE game_id=? AND player_id=? AND created_date=? LIMIT 1",
        (game_id, player_id, date)).fetchone())


def _date_minus_one(date):
    try:
        d = datetime.date.fromisoformat(date) - datetime.timedelta(days=1)
        return d.isoformat()
    except ValueError:
        return date


# ============================================================
# 开局
# ============================================================

def _normalize_selected(symbols):
    out = []
    for item in symbols or []:
        raw = item.get("symbol") if isinstance(item, dict) else item
        try:
            s = normalize_symbol(raw)
        except StockError:
            continue
        if s not in out:
            out.append(s)
    if len(out) < MIN_SYMBOLS:
        raise StockError("至少选择 %d 个标的" % MIN_SYMBOLS)
    if len(out) > MAX_SYMBOLS:
        raise StockError("最多选择 %d 个标的" % MAX_SYMBOLS)
    if len({is_crypto(s) for s in out}) > 1:
        raise StockError("同一局不能混选 A 股与币市")
    return out


def _calendar_from(benchmark_bars, days, end_date="", label="沪深300"):
    bars = [b for b in benchmark_bars if not end_date or b["date"] <= end_date]
    dates = sorted({b["date"] for b in bars})
    if len(dates) < 3:
        raise StockError("%s历史行情不足，暂时无法开局" % label)
    days = max(1, min(int(days or 20), 60))
    need = days + 1
    if len(dates) < need:
        days = max(1, len(dates) - 1)
        need = days + 1
    window = dates[-need:]
    return window[0], window[-1], days


def _fetch_all_bars(symbols, count, bars_by_symbol=None):
    out = {}
    for s in symbols:
        if bars_by_symbol is not None:
            arr = bars_by_symbol.get(s) or []
        else:
            arr = fetch_daily(s, count)
        cleaned = [_bar_row(x.get("date"), x.get("open"), x.get("close"),
                            x.get("high"), x.get("low"), x.get("volume"),
                            x.get("amount") or 0)
                   for x in arr if isinstance(x, dict) and x.get("date")]
        cleaned.sort(key=lambda x: x["date"])
        if len(cleaned) < 2:
            raise StockError("%s 没有足够的日 K 数据" % s)
        out[s] = cleaned
    return out


def _symbol_names(symbols, bars_by_symbol=None, names=None):
    if names:
        return {s: str(names.get(s) or s) for s in symbols}
    if bars_by_symbol is not None:
        return {s: s for s in symbols}
    try:
        quotes = fetch_quotes(symbols)
    except StockError:
        quotes = {}
    return {s: (quotes.get(s) or {}).get("name") or s for s in symbols}


def create_game(mode="replay", symbols=None, days=20, ai_count=3,
                initial_cash=1000000, name="", end_date="",
                bars_by_symbol=None, benchmark_bars=None, names=None,
                market="stock"):
    """开一局。返回完整 state。

    market: stock（A 股 / ETF）| crypto（币市）。
    bars_by_symbol / benchmark_bars / names 是测试注入口；正常调用不传，
    由腾讯 / 东财 / Binance 行情自动拉取。
    """
    init_db()
    mode = "live" if str(mode) == "live" else "replay"
    market = "crypto" if str(market or "").lower() in ("crypto", "coin", "币市", "币") else "stock"
    if market == "crypto" and not symbols:
        symbols = list(CRYPTO_DEFAULT)
    syms = _normalize_selected(symbols)
    if market == "crypto" and not all(is_crypto(s) for s in syms):
        raise StockError("币市只能选择 BTC / ETH / SOL")
    if market == "stock" and any(is_crypto(s) for s in syms):
        raise StockError("A 股盘不能加入币市标的")
    bm_symbol = CRYPTO_BENCHMARK if market == "crypto" else BENCHMARK_SYMBOL
    bm_name = CRYPTO_BENCHMARK_NAME if market == "crypto" else BENCHMARK_NAME
    fees = CRYPTO_FEES if market == "crypto" else DEFAULT_FEES
    try:
        ai_count = max(0, min(int(ai_count if ai_count is not None else 3), MAX_AI))
    except (TypeError, ValueError):
        ai_count = 3
    cash0 = max(100000.0, min(float(initial_cash or 1000000), 100000000.0))
    days = max(1, min(int(days or 20), 60))

    names_map = _symbol_names(syms, bars_by_symbol=bars_by_symbol, names=names)
    count = days + 140 if mode == "replay" else 160
    bars_map = _fetch_all_bars(syms, count, bars_by_symbol=bars_by_symbol)
    for s in bars_map:
        # 币市日线在抓取时已按 close_time 丢掉未收盘的那根，不再套 A 股 15:05 的规则
        if not is_crypto(s):
            bars_map[s] = _drop_partial_today(bars_map[s])

    if benchmark_bars is None:
        bm = fetch_benchmark(count=count, symbol=bm_symbol)
    else:
        bm = [_bar_row(x.get("date"), x.get("open"), x.get("close"),
                       x.get("high"), x.get("low"), x.get("volume"),
                       x.get("amount") or 0)
              for x in benchmark_bars if isinstance(x, dict) and x.get("date")]
    bm.sort(key=lambda x: x["date"])
    if not is_crypto(bm_symbol):
        bm = _drop_partial_today(bm)
    if mode == "live":
        if not bm:
            raise StockError("%s没有可用行情" % bm_name)
        cursor = bm[-1]["date"]
        if not end_date:
            end_date = ""
        # 实时盘：只存到当前已收盘日，绝不把未来数据写进快照
        bars_map = {s: [b for b in arr if b["date"] <= cursor] for s, arr in bars_map.items()}
        bm = [b for b in bm if b["date"] <= cursor]
        start_date = cursor
    else:
        start_date, end_date, days = _calendar_from(bm, days, end_date=end_date,
                                                    label=bm_name)
        cursor = start_date
        bm = [b for b in bm if b["date"] <= end_date]
        bars_map = {s: [b for b in arr if b["date"] <= end_date]
                    for s, arr in bars_map.items()}

    for s, arr in bars_map.items():
        if not arr:
            raise StockError("%s 在所选区间内没有行情" % s)
    if not bm:
        raise StockError("所选区间内没有%s行情" % bm_name)

    game_id = "st_" + uuid.uuid4().hex[:12]
    if not name:
        prefix = ("币市回放 " if market == "crypto" else "历史回放 ") if mode == "replay" \
            else ("币市实盘 " if market == "crypto" else "实时每日盘 ")
        name = prefix + cursor
    now = time.time()
    conn = get_db()
    with _GAME_LOCK:
        conn.execute(
            "INSERT INTO games(id,name,mode,market,status,created_at,start_date,end_date,"
            "cursor_date,initial_cash,commission,min_commission,stamp_duty,transfer_fee,"
            "slippage,benchmark) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (game_id, str(name)[:80], mode, market, "running", now, start_date, end_date,
             cursor, cash0, fees["commission"], fees["min_commission"],
             fees["stamp_duty"], fees["transfer_fee"],
             fees["slippage"], bm_symbol))
        for s in syms:
            kind = "crypto" if is_crypto(s) else ("etf" if is_etf(s) else "stock")
            conn.execute(
                "INSERT OR REPLACE INTO symbols(game_id,symbol,name,kind,tradable) "
                "VALUES(?,?,?,?,1)", (game_id, s, names_map.get(s) or s, kind))
        # 基准通常不参与交易；但币市里 BTC 既是基准也是可交易标的，
        # 这时不能再用 tradable=0 的基准行把它的交易行覆盖掉。
        if bm_symbol not in syms:
            conn.execute(
                "INSERT OR REPLACE INTO symbols(game_id,symbol,name,kind,tradable) "
                "VALUES(?,?,?,?,0)", (game_id, bm_symbol, bm_name, "benchmark"))
        rows = []
        for s in list(syms) + [bm_symbol]:
            for b in (bm if s == bm_symbol else bars_map[s]):
                rows.append((game_id, s, b["date"], b["open"], b["high"], b["low"],
                             b["close"], b["volume"], b.get("amount") or 0))
        conn.executemany(
            "INSERT OR REPLACE INTO bars(game_id,symbol,date,open,high,low,close,volume,amount) "
            "VALUES(?,?,?,?,?,?,?,?,?)", rows)
        players = [("you", "你", "human", "", cash0)]
        for p in PERSONAS[:ai_count]:
            players.append((p["id"], p["name"], "ai", p["id"], cash0))
        for pid, pname, kind, persona, cash in players:
            conn.execute(
                "INSERT INTO players(id,game_id,name,kind,persona,cash,initial_cash,realized_pnl) "
                "VALUES(?,?,?,?,?,?,?,0)",
                (pid, game_id, pname, kind, persona, cash, cash))
        conn.commit()
        for pid, _n, _k, _p, _c in players:
            _snapshot(conn, game_id, _player(conn, game_id, pid), cursor)
        conn.commit()
    return game_state(game_id)


def add_symbol(game_id, raw, bars_by_symbol=None, names=None):
    """中途给已有对局加入一只标的（模拟真实股市里随时发现新机会）。

    回放模式补齐 start_date ~ end_date 的日 K，保证后续交易日能正常撮合；
    实时模式只写 <= cursor_date，绝不提前拿到未来。
    """
    init_db()
    with _GAME_LOCK:
        conn = get_db()
        game = _game(conn, game_id)
        if game["status"] != "running":
            raise StockError("这一局已经结束了")
        s = normalize_symbol(raw)
        crypto_game = str(game.get("market") or "stock") == "crypto"
        if crypto_game and not is_crypto(s):
            raise StockError("币市对局只能加入 BTC / ETH / SOL")
        if not crypto_game and is_crypto(s):
            raise StockError("A 股对局不能加入币市标的")
        if conn.execute("SELECT 1 FROM symbols WHERE game_id=? AND symbol=?",
                        (game_id, s)).fetchone():
            raise StockError("这只标的已经在观察池里了")
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM symbols WHERE game_id=? AND tradable=1",
            (game_id,)).fetchone()["n"]
        if int(count) >= MAX_SYMBOLS:
            raise StockError("最多 %d 只标的" % MAX_SYMBOLS)

        try:
            arr = _fetch_all_bars([s], 400, bars_by_symbol=bars_by_symbol)[s]
        except StockError as e:
            raise StockError("拿不到 %s 的行情：%s" % (s, e))
        if game["mode"] == "live":
            arr = _drop_partial_today(arr)
            arr = [b for b in arr if b["date"] <= game["cursor_date"]]
        else:
            start = game["start_date"]
            end = game["end_date"] or "9999-12-31"
            arr = [b for b in arr if start <= b["date"] <= end]
        if len(arr) < 2:
            raise StockError("%s 在本局区间内没有足够行情" % s)

        name = (names or {}).get(s)
        if not name and bars_by_symbol is None:
            try:
                name = (fetch_quotes([s]).get(s) or {}).get("name")
            except StockError:
                name = None
        name = name or s
        kind = "crypto" if is_crypto(s) else ("etf" if is_etf(s) else "stock")
        conn.execute(
            "INSERT OR REPLACE INTO symbols(game_id,symbol,name,kind,tradable) "
            "VALUES(?,?,?,?,1)", (game_id, s, name, kind))
        conn.executemany(
            "INSERT OR REPLACE INTO bars(game_id,symbol,date,open,high,low,close,volume,amount) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(game_id, s, b["date"], b["open"], b["high"], b["low"], b["close"],
              b["volume"], b.get("amount") or 0) for b in arr])
        conn.commit()
        return {"game": game_state(game_id), "symbol": s, "name": name}


def _summary(conn, g):
    human = _row(conn.execute(
        "SELECT * FROM players WHERE game_id=? AND kind='human' LIMIT 1",
        (g["id"],)).fetchone())
    snap = None
    if human:
        snap = _row(conn.execute(
            "SELECT * FROM snapshots WHERE game_id=? AND player_id=? AND date=?",
            (g["id"], human["id"], g["cursor_date"])).fetchone())
    equity = float(snap["equity"]) if snap else float((human or {}).get("cash") or 0)
    init = float((human or {}).get("initial_cash") or 1) or 1
    return {
        "id": g["id"], "name": g["name"], "mode": g["mode"],
        "market": str(g.get("market") or "stock"),
        "status": g["status"], "created_at": g["created_at"],
        "start_date": g["start_date"], "cursor_date": g["cursor_date"],
        "end_date": g["end_date"], "equity": round(equity, 2),
        "return_pct": round((equity / init - 1.0) * 100, 2),
    }


def list_games(limit=20):
    init_db()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM games ORDER BY created_at DESC LIMIT ?",
        (max(1, min(int(limit or 20), 100)),)).fetchall()
    return [_summary(conn, _row(r)) for r in rows]


def delete_game(game_id):
    init_db()
    with _GAME_LOCK:
        conn = get_db()
        _game(conn, game_id)
        for table in ("players", "symbols", "bars", "orders", "positions",
                      "trades", "snapshots", "journals",
                      "analyses", "analysis_symbols",
                      "analyses_cold", "analysis_symbols_cold", "predictions"):
            conn.execute("DELETE FROM " + table + " WHERE game_id=?", (game_id,))
        conn.execute("DELETE FROM games WHERE id=?", (game_id,))
        conn.commit()
        rebuild_analyses_fts(conn)
    return {"ok": True}


# ============================================================
# 查询 / 状态
# ============================================================

def _pending_orders(conn, game_id, player_id):
    rows = conn.execute(
        "SELECT o.*, s.name FROM orders o LEFT JOIN symbols s "
        "ON s.game_id=o.game_id AND s.symbol=o.symbol "
        "WHERE o.game_id=? AND o.player_id=? AND o.status='pending' "
        "ORDER BY o.created_at", (game_id, player_id)).fetchall()
    return [_row(r) for r in rows]


def _recent_trades(conn, game_id, player_id, limit=20):
    rows = conn.execute(
        "SELECT t.*, s.name FROM trades t LEFT JOIN symbols s "
        "ON s.game_id=t.game_id AND s.symbol=t.symbol "
        "WHERE t.game_id=? AND t.player_id=? ORDER BY t.created_at DESC LIMIT ?",
        (game_id, player_id, max(1, int(limit)))).fetchall()
    return [_row(r) for r in rows]


def _player_view(conn, game, p, date, positions=None, decided=None):
    """单个参与者的资产视图。positions / decided 可由调用方批量预取后传进来，
    这样看排行榜时不再「每人一段查询」。"""
    pos_rows = positions if positions is not None else _positions_with_last(
        conn, game["id"], p["id"], date)
    cash = float(p["cash"])
    pos_out = []
    mv = 0.0
    for pos in pos_rows:
        qty = _qnum(pos["qty"])
        last = float(pos["last"] or 0)
        value = qty * last
        mv += value
        avg = float(pos["avg_cost"] or 0)
        pos_out.append({
            "symbol": pos["symbol"],
            "qty": qty,
            "available_qty": _qnum(pos["available_qty"]),
            "avg_cost": round(avg, 3),
            "last": round(last, 3),
            "market_value": round(value, 2),
            "pnl_pct": round((last / avg - 1.0) * 100, 2) if avg > 0 else 0.0,
        })
    equity = cash + mv
    init = float(p["initial_cash"]) or 1.0
    if decided is None:
        decided = _journal_exists(conn, game["id"], p["id"], date)
    return {
        "id": p["id"], "name": p["name"], "kind": p["kind"], "persona": p["persona"],
        "cash": round(cash, 2), "market_value": round(mv, 2),
        "equity": round(equity, 2), "return_pct": round((equity / init - 1.0) * 100, 2),
        "realized_pnl": round(float(p["realized_pnl"] or 0), 2),
        "positions": pos_out,
        "decided": bool(decided),
    }


def _players(conn, game_id):
    return [_row(r) for r in conn.execute(
        "SELECT * FROM players WHERE game_id=? ORDER BY rowid", (game_id,)).fetchall()]


def _positions_all_with_last(conn, game_id, date):
    """全桌持仓一次取回，按参与者分组。"""
    out = {}
    for r in conn.execute(
            "SELECT p.player_id AS pid, p.symbol, p.qty, p.available_qty, p.avg_cost, "
            "(SELECT b.close FROM bars b WHERE b.game_id=p.game_id AND b.symbol=p.symbol "
            " AND b.date<=? ORDER BY b.date DESC LIMIT 1) AS last "
            "FROM positions p WHERE p.game_id=? AND p.qty>0 ORDER BY p.player_id",
            (date, game_id)).fetchall():
        out.setdefault(r["pid"], []).append(_row(r))
    return out


def _decided_set(conn, game_id, date):
    return {r["player_id"] for r in conn.execute(
        "SELECT DISTINCT player_id FROM journals WHERE game_id=? AND date=?",
        (game_id, date)).fetchall()}


def _players_view(conn, game, date):
    """排行榜整桌视图：固定 3 条 SQL（参与者 / 持仓 / 已决策），与人数无关。"""
    players = _players(conn, game["id"])
    positions = _positions_all_with_last(conn, game["id"], date)
    decided = _decided_set(conn, game["id"], date)
    return [_player_view(conn, game, p, date,
                         positions=positions.get(p["id"]), decided=p["id"] in decided)
            for p in players]


def _symbols_view(conn, game_id, cursor):
    """观察池 + 现价/涨跌幅：一条 SQL（原来每只标的要查两次 bars）。"""
    rows = conn.execute(
        "SELECT s.symbol, s.name, s.kind, "
        "(SELECT b.close FROM bars b WHERE b.game_id=s.game_id AND b.symbol=s.symbol "
        " AND b.date<=? ORDER BY b.date DESC LIMIT 1) AS last, "
        "(SELECT b.close FROM bars b WHERE b.game_id=s.game_id AND b.symbol=s.symbol "
        " AND b.date<? ORDER BY b.date DESC LIMIT 1) AS prev "
        "FROM symbols s WHERE s.game_id=? AND s.tradable=1 ORDER BY s.rowid",
        (cursor, cursor, game_id)).fetchall()
    out = []
    for r in rows:
        last = float(r["last"] or 0)
        prev = float(r["prev"] or 0)
        change = (last / prev - 1.0) * 100 if prev else 0.0
        out.append({"symbol": r["symbol"], "name": r["name"], "kind": r["kind"],
                    "last": round(last, 3), "change_pct": round(change, 2)})
    return out


def _journals_map(conn, game_id, cursor=None, limit=5):
    """每个 AI 的最近 N 条复盘：一条 SQL 取回后再按人切片。"""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM players WHERE game_id=? AND kind='ai' ORDER BY rowid",
        (game_id,)).fetchall()]
    out = {pid: [] for pid in ids}
    if not ids:
        return out
    sql = "SELECT player_id,date,text FROM journals WHERE game_id=?"
    args = [game_id]
    if cursor:
        sql += " AND date<=?"
        args.append(cursor)
    sql += " ORDER BY date DESC"
    for r in conn.execute(sql, tuple(args)).fetchall():
        bucket = out.get(r["player_id"])
        if bucket is not None and len(bucket) < limit:
            bucket.append({"date": r["date"], "text": r["text"]})
    return out


def _bm(game):
    """本局的基准代码：A 股 = 沪深300，币市 = BTC。"""
    return str((game or {}).get("benchmark") or BENCHMARK_SYMBOL)


def _bm_name(game):
    return CRYPTO_BENCHMARK_NAME if is_crypto(_bm(game)) else BENCHMARK_NAME


def _benchmark_stats(conn, game, date):
    bm = _bm(game)
    start = _last_close(conn, game["id"], bm, game["start_date"])
    now = _last_close(conn, game["id"], bm, date)
    return {
        "symbol": bm, "name": _bm_name(game),
        "start_close": round(start, 2), "current_close": round(now, 2),
        "return_pct": round((now / start - 1.0) * 100, 2) if start > 0 else 0.0,
    }


def _next_date(conn, game):
    if game["mode"] == "live":
        return ""
    r = conn.execute(
        "SELECT MIN(date) AS d FROM bars WHERE game_id=? AND symbol=? "
        "AND date>? AND date<=?",
        (game["id"], game["benchmark"], game["cursor_date"],
         game["end_date"] or "9999-12-31")).fetchone()
    return (r["d"] or "") if r else ""


def _remaining(conn, game):
    if game["mode"] == "live":
        return None
    r = conn.execute(
        "SELECT COUNT(DISTINCT date) AS n FROM bars WHERE game_id=? AND symbol=? "
        "AND date>? AND date<=?",
        (game["id"], game["benchmark"], game["cursor_date"],
         game["end_date"] or "9999-12-31")).fetchone()
    return int((r["n"] if r else 0) or 0)


def game_state(game_id):
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        cursor = game["cursor_date"]
        pviews = _players_view(conn, game, cursor)
        human = next((p for p in pviews if p["kind"] == "human"), None)
        ai_players = [p for p in pviews if p["kind"] == "ai"]
        return {
            "id": game["id"], "name": game["name"], "mode": game["mode"],
            "market": str(game.get("market") or "stock"),
            "status": game["status"], "start_date": game["start_date"],
            "end_date": game["end_date"], "cursor_date": cursor,
            "next_date": _next_date(conn, game), "remaining_days": _remaining(conn, game),
            "initial_cash": round(float(game["initial_cash"]), 2),
            "symbols": _symbols_view(conn, game_id, cursor), "players": pviews,
            "leaderboard": sorted(pviews, key=lambda x: x["equity"], reverse=True),
            "pending_orders": _pending_orders(conn, game_id, human["id"]) if human else [],
            "trades": _recent_trades(conn, game_id, human["id"], 20) if human else [],
            "journals": _journals_map(conn, game_id, cursor, limit=5),
            "benchmark": _benchmark_stats(conn, game, cursor),
            "regime": _regime(conn, game_id, cursor),
            "ai_ready": all(p["decided"] for p in ai_players),
            "ai_count": len(ai_players),
            "fees": {
                "commission": game["commission"], "min_commission": game["min_commission"],
                "stamp_duty": game["stamp_duty"], "transfer_fee": game["transfer_fee"],
                "slippage": game["slippage"],
            },
        }


# 增量同步：前端每次操作只回变化的切片，不再整份 state 重拉 + 整页重绘。
# scope 取值："orders"（下单/撤单） / "ai"（AI 决策） / "all"（推进日、重开界面）
def change_patch(game_id, scope="all", player_id=""):
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        cursor = game["cursor_date"]
        scope = str(scope or "all").lower()
        patch = {
            "id": game_id, "scope": scope, "cursor_date": cursor,
            "status": game["status"], "next_date": _next_date(conn, game),
            "remaining_days": _remaining(conn, game),
            "regime": _regime(conn, game_id, cursor),
        }
        need_all = scope not in ("orders", "ai")
        pviews = _players_view(conn, game, cursor)
        ai_players = [p for p in pviews if p["kind"] == "ai"]
        if scope == "ai" and player_id:
            patch["players"] = [p for p in pviews if p["id"] == player_id]
            patch["journals"] = _journals_map(conn, game_id, cursor, limit=5)
        elif scope == "orders":
            patch["players"] = [p for p in pviews if p["kind"] == "human"]
            human = next((p for p in pviews if p["kind"] == "human"), None)
            patch["pending_orders"] = _pending_orders(conn, game_id, human["id"]) if human else []
        else:
            human = next((p for p in pviews if p["kind"] == "human"), None)
            patch["players"] = pviews
            patch["leaderboard"] = sorted(pviews, key=lambda x: x["equity"], reverse=True)
            patch["symbols"] = _symbols_view(conn, game_id, cursor)
            patch["pending_orders"] = _pending_orders(conn, game_id, human["id"]) if human else []
            patch["trades"] = _recent_trades(conn, game_id, human["id"], 20) if human else []
            patch["journals"] = _journals_map(conn, game_id, cursor, limit=5)
            patch["benchmark"] = _benchmark_stats(conn, game, cursor)
        if need_all or scope == "ai":
            patch["ai_ready"] = all(p["decided"] for p in ai_players)
            patch["ai_count"] = len(ai_players)
        return patch


def bars_for(game_id, symbol, limit=120, date="", with_marks=False):
    """日 K。date 给定时按「那个时刻」取（回溯模式用）；with_marks 时带该标的成交点。"""
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        s = normalize_symbol(symbol)
        if not conn.execute(
                "SELECT 1 FROM symbols WHERE game_id=? AND symbol=?",
                (game_id, s)).fetchone():
            raise StockError("这只标的不在本局观察池里")
        cursor = game["cursor_date"]
        as_of = str(date or "")[:10] or cursor
        if as_of > cursor:
            as_of = cursor
        if as_of < game["start_date"]:
            as_of = game["start_date"]
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume FROM bars "
            "WHERE game_id=? AND symbol=? AND date<=? ORDER BY date DESC LIMIT ?",
            (game_id, s, as_of, max(10, min(int(limit or 120), 240)))).fetchall()
        out = [dict(r) for r in reversed(rows)]
        if with_marks:
            return {"as_of": as_of, "bars": out,
                    "marks": _trades_of(conn, game_id, symbol=s, date_to=as_of, limit=400)}
        return out


def _trades_of(conn, game_id, player_id="", symbol="", date_from="", date_to="", limit=200):
    """成交查询：K 线标注、历史交易台账共用。

    一并带上玩家名与类型（human/ai），否则图上只看到「买100股」，
    分不清是自己还是哪个 AI 干的。
    """
    sql = ("SELECT t.*, s.name, p.name AS player_name, p.kind AS player_kind "
           "FROM trades t "
           "LEFT JOIN symbols s ON s.game_id=t.game_id AND s.symbol=t.symbol "
           "LEFT JOIN players p ON p.game_id=t.game_id AND p.id=t.player_id "
           "WHERE t.game_id=?")
    args = [game_id]
    if player_id:
        sql += " AND t.player_id=?"
        args.append(player_id)
    if symbol:
        sql += " AND t.symbol=?"
        args.append(normalize_symbol(symbol))
    if date_from:
        sql += " AND t.date>=?"
        args.append(str(date_from)[:10])
    if date_to:
        sql += " AND t.date<=?"
        args.append(str(date_to)[:10])
    sql += " ORDER BY t.date ASC, t.created_at ASC LIMIT ?"
    args.append(max(1, min(int(limit or 200), 2000)))
    return [_row(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def trades_for(game_id, player_id="", symbol="", date_from="", date_to="", limit=200):
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        date_to = str(date_to or "")[:10] or game["cursor_date"]
        return _trades_of(conn, game_id, player_id=player_id, symbol=symbol,
                          date_from=date_from, date_to=date_to, limit=limit)


# ============================================================
# AI 分析记录：写入 / 多维检索 / 冷热归档
# ============================================================

ANALYSIS_KINDS = ("大盘", "个股", "持仓", "风险", "机会", "复盘")


def analysis_kinds():
    return list(ANALYSIS_KINDS)


def _norm_kind(v):
    k = str(v or "").strip()
    return k if k in ANALYSIS_KINDS else "复盘"


def _norm_keywords(v):
    if isinstance(v, (list, tuple, set)):
        items = list(v)
    else:
        items = str(v or "").replace("，", ",").replace("；", ",").replace("、", ",").split(",")
    out = []
    for k in items:
        k = str(k).strip()
        if k and k not in out:
            out.append(k)
    return out


def _norm_symbols(v):
    out = []
    for s in (v or []):
        try:
            s = normalize_symbol(s)
        except StockError:
            continue
        if s not in out:
            out.append(s)
    return out


def _insert_analysis(conn, game_id, player_id, date, kind, text, keywords, symbols):
    """写一条 AI 分析：热表 + 标的关联 + 全文索引，事务由调用方负责。"""
    aid = "an_" + uuid.uuid4().hex[:12]
    kind = _norm_kind(kind)
    text = str(text or "").strip()[:2000]
    kws = ",".join(_norm_keywords(keywords))[:200]
    syms = _norm_symbols(symbols)
    cur = conn.execute(
        "INSERT INTO analyses(id,game_id,player_id,date,kind,text,keywords,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (aid, game_id, player_id, str(date)[:10], kind, text, kws, time.time()))
    if syms:
        conn.executemany(
            "INSERT OR IGNORE INTO analysis_symbols(game_id,analysis_id,symbol) VALUES(?,?,?)",
            [(game_id, aid, s) for s in syms])
    _fts_insert(conn, cur.lastrowid, text, kws)
    return {"id": aid, "game_id": game_id, "player_id": player_id, "date": str(date)[:10],
            "kind": kind, "text": text, "keywords": kws, "symbols": syms}


def _symbols_of_analyses(conn, ids):
    """一次取回一批分析的关联标的，避免逐条查询。"""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    sql = ("SELECT analysis_id,symbol FROM analysis_symbols WHERE analysis_id IN (%s) "
           "UNION ALL SELECT analysis_id,symbol FROM analysis_symbols_cold "
           "WHERE analysis_id IN (%s)") % (marks, marks)
    out = {}
    for r in conn.execute(sql, tuple(list(ids) + list(ids))).fetchall():
        out.setdefault(r["analysis_id"], []).append(r["symbol"])
    return out


def _analysis_src(table, sym_table, use_fts, game_id, q, kind, player_id,
                  date_from, date_to, symbol):
    """构造「分析记录」查询的 FROM + WHERE（取页与计数共用一个来源定义）。

    两个关键性能点：
    1) 按标的筛选用 analysis_symbols 做驱动表（走 (game_id, symbol) 索引），
       而不是从百万行的 analyses 出发再逐行回查 —— 后者是 9.5 秒级的全表回查。
    2) 关键词用 FTS（trigram）或 LIKE，其余结构化条件走索引。
    """
    if symbol and sym_table:
        frm = "%s sy JOIN %s a ON a.id=sy.analysis_id" % (sym_table, table)
    else:
        frm = "%s a" % table
    if use_fts:
        frm += " JOIN analyses_fts f ON f.rowid=a.rowid"
    conds = []
    args = []
    if use_fts:
        conds.append("f.analyses_fts MATCH ?")
        args.append('"%s"' % q.replace('"', " "))
    elif q:
        conds.append("(a.text LIKE ? OR a.keywords LIKE ?)")
        args += ["%" + q + "%", "%" + q + "%"]
    if symbol and sym_table:
        conds.append("sy.game_id=?")
        args.append(game_id)
        conds.append("sy.symbol=?")
        args.append(symbol)
    if game_id:
        conds.append("a.game_id=?")
        args.append(game_id)
    if kind:
        conds.append("a.kind=?")
        args.append(kind)
    if player_id:
        conds.append("a.player_id=?")
        args.append(player_id)
    if date_from:
        conds.append("a.date>=?")
        args.append(date_from)
    if date_to:
        conds.append("a.date<=?")
        args.append(date_to)
    return frm, (" AND ".join(conds) if conds else "1=1"), args


def _has_rows(conn, table, game_id):
    """来源表里这局有没有记录：一条探测 SQL，省掉没必要的 UNION 分支。"""
    if game_id:
        r = conn.execute("SELECT 1 FROM %s WHERE game_id=? LIMIT 1" % table,
                         (game_id,)).fetchone()
    else:
        r = conn.execute("SELECT 1 FROM %s LIMIT 1" % table).fetchone()
    return bool(r)


# 命中数封顶：精确 COUNT 在大结果集上要遍历全部命中行，百万级下会拖到秒级；
# 封顶后只需扫到上限就停，代价有界，前端按「2 万+」展示即可。
ANALYSIS_COUNT_CAP = 20001


def search_analyses(game_id="", q="", kind="", symbol="", player_id="",
                    date_from="", date_to="", page=1, size=20):
    """按时间 / 类型 / 标的 / 关键词多维组合筛选，分页返回。

    冷热两表对调用方透明（UNION ALL），实际只有一侧有数据时自动退化成单表查询。
    """
    init_db()
    conn = get_db()
    page = max(1, int(page or 1))
    size = max(1, min(int(size or 20), 100))
    q = str(q or "").strip()
    kind = str(kind or "").strip()
    date_from = str(date_from or "")[:10]
    date_to = str(date_to or "")[:10]
    game_id = str(game_id or "").strip()
    empty = {"items": [], "total": 0, "page": page, "size": size, "pages": 0,
             "has_more": False, "total_capped": False, "took_ms": 0.0}
    sym = ""
    if symbol:
        try:
            sym = normalize_symbol(symbol)
        except StockError:
            return dict(empty, engine=_FTS_MODE or "like")
    use_fts = bool(q) and len(q) >= _FTS_MIN_LEN and _FTS_MODE == "trigram"
    cols = "a.id,a.player_id,a.date,a.kind,a.text,a.keywords,a.created_at"
    parts, args = [], []
    for table, sym_table in (("analyses", "analysis_symbols"),
                             ("analyses_cold", "analysis_symbols_cold")):
        if not _has_rows(conn, table, game_id):
            continue
        frm, cond, a = _analysis_src(table, sym_table, use_fts and table == "analyses",
                                     game_id, q, kind, player_id, date_from, date_to, sym)
        parts.append("SELECT %s FROM %s WHERE %s" % (cols, frm, cond))
        args += a
    if not parts:
        return dict(empty, engine=_FTS_MODE or "like")
    t0 = time.perf_counter()
    union = " UNION ALL ".join(parts)
    rows = conn.execute(
        "SELECT * FROM (%s) ORDER BY date DESC, created_at DESC LIMIT ? OFFSET ?" % union,
        tuple(args + [size, (page - 1) * size])).fetchall()
    total = int(conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM (%s) LIMIT ?)" % union,
        tuple(args + [ANALYSIS_COUNT_CAP])).fetchone()["n"])
    capped = total >= ANALYSIS_COUNT_CAP
    items = [_row(r) for r in rows]
    sym_map = _symbols_of_analyses(conn, [x["id"] for x in items])
    names = {}
    if game_id:
        names = {r["id"]: r["name"] for r in conn.execute(
            "SELECT id,name FROM players WHERE game_id=?", (game_id,)).fetchall()}
    for x in items:
        x["symbols"] = sym_map.get(x["id"], [])
        x["player_name"] = names.get(x["player_id"], x["player_id"])
    return {"items": items, "total": total, "page": page, "size": size,
            "pages": (total + size - 1) // size,
            "has_more": len(items) >= size,
            "total_capped": capped,
            "took_ms": round((time.perf_counter() - t0) * 1000, 2),
            "engine": _FTS_MODE or "like"}


def _chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _archive_analyses_tx(conn, game_id, keep_days=0):
    """冷热搬迁本体：不动事务、不重建索引，交给调用方收尾。

    热表保留 FTS 索引（检索快），冷表只用索引 + LIKE，查询时 UNION。
    """
    rows = [_row(r) for r in conn.execute(
        "SELECT * FROM analyses WHERE game_id=?", (game_id,)).fetchall()]
    if not rows:
        return 0
    newest = max(r["date"] for r in rows)
    if int(keep_days or 0) > 0:
        keep_from = datetime.date.fromisoformat(newest) - datetime.timedelta(
            days=int(keep_days))
        cut = keep_from.isoformat()
    else:
        cut = newest
    move = [r for r in rows if r["date"] <= cut]
    ids = [r["id"] for r in move]
    if not ids:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO analyses_cold"
        "(id,game_id,player_id,date,kind,text,keywords,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [(r["id"], r["game_id"], r["player_id"], r["date"], r["kind"],
          r["text"], r["keywords"], r["created_at"]) for r in move])
    for chunk in _chunks(ids, 400):
        marks = ",".join("?" * len(chunk))
        conn.execute(
            "INSERT OR REPLACE INTO analysis_symbols_cold"
            "(game_id,analysis_id,symbol) SELECT game_id,analysis_id,symbol "
            "FROM analysis_symbols WHERE analysis_id IN (%s)" % marks, tuple(chunk))
        conn.execute("DELETE FROM analysis_symbols WHERE analysis_id IN (%s)" % marks,
                     tuple(chunk))
    conn.execute("DELETE FROM analyses WHERE game_id=? AND date<=?", (game_id, cut))
    return len(ids)


def archive_analyses(game_id, keep_days=0):
    """冷热分离：把已结束对局的旧分析搬进对调用方透明的冷表。"""
    init_db()
    with _GAME_LOCK:
        conn = get_db()
        game = _game(conn, game_id)
        if game["status"] != "finished":
            raise StockError("只有已结束的对局才做冷热归档")
        moved = _archive_analyses_tx(conn, game_id, keep_days)
        conn.commit()
        if moved:
            rebuild_analyses_fts(conn)
    return {"moved": moved, "game_id": game_id}


# ============================================================
# 时间轴回溯 / 历史对比
# ============================================================

def _clamp_date(game, date):
    """把用户给的时间点夹到合法区间，并禁止越过当前模拟日（不偷看未来）。"""
    d = str(date or "")[:10]
    if not d:
        return game["cursor_date"]
    try:
        datetime.date.fromisoformat(d)
    except ValueError:
        raise StockError("时间点格式不对，应为 YYYY-MM-DD")
    if d > game["cursor_date"]:
        return game["cursor_date"]
    if d < game["start_date"]:
        return game["start_date"]
    return d


def _last_closes(conn, game_id, as_of):
    return {r["symbol"]: float(r["close"] or 0) for r in conn.execute(
        "SELECT s.symbol, (SELECT b.close FROM bars b WHERE b.game_id=s.game_id "
        " AND b.symbol=s.symbol AND b.date<=? ORDER BY b.date DESC LIMIT 1) AS close "
        "FROM symbols s WHERE s.game_id=?", (as_of, game_id)).fetchall()}


def _replay_positions(conn, game_id, date):
    """按成交流水重放到 date 日终：现金、持仓、可卖、成本价、已实现盈亏。

    记账口径与 _execute_pending 完全一致（手续费计入成本、T+1 次日解锁），
    所以回溯出来的持仓/资产与当天真实结算结果一致。
    """
    players = {p["id"]: {"cash": float(p["initial_cash"]), "realized_pnl": 0.0,
                         "positions": {}} for p in _players(conn, game_id)}
    rows = conn.execute(
        "SELECT player_id,symbol,side,date,qty,price,fee,amount FROM trades "
        "WHERE game_id=? AND date<=? ORDER BY date ASC, created_at ASC",
        (game_id, date)).fetchall()
    cur_date = ""
    for r in rows:
        st = players.get(r["player_id"])
        if st is None:
            continue
        if r["date"] != cur_date:
            cur_date = r["date"]
            for p in players.values():                      # T+1：换日解锁
                for pos in p["positions"].values():
                    pos["available_qty"] = pos["qty"]
        qty = float(r["qty"])
        price = float(r["price"] or 0)
        fee = float(r["fee"] or 0)
        amount = float(r["amount"] or 0) or price * qty
        pos = st["positions"].get(r["symbol"])
        if r["side"] == "buy":
            old_qty = float(pos["qty"]) if pos else 0.0
            old_avg = float(pos["avg_cost"]) if pos else 0.0
            new_qty = old_qty + qty
            if is_crypto(r["symbol"]):
                new_qty = round(new_qty, CRYPTO_QTY_DECIMALS)
            new_avg = ((old_qty * old_avg) + amount + fee) / new_qty if new_qty else 0.0
            st["cash"] -= (amount + fee)
            avail_new = float(pos["available_qty"]) if pos else 0.0
            if is_crypto(r["symbol"]):      # 币市无 T+1：回放口径也要一致
                avail_new = new_qty
            st["positions"][r["symbol"]] = {
                "symbol": r["symbol"], "qty": new_qty,
                "available_qty": avail_new,
                "avg_cost": new_avg}
        else:
            if not pos:
                continue
            st["cash"] += (amount - fee)
            st["realized_pnl"] += (price - float(pos["avg_cost"])) * qty - fee
            left = float(pos["qty"]) - qty
            if is_crypto(r["symbol"]):
                left = round(left, CRYPTO_QTY_DECIMALS)
            if left > 0:
                pos["qty"] = left
                pos["available_qty"] = max(0.0, float(pos["available_qty"]) - qty)
            else:
                st["positions"].pop(r["symbol"], None)
    return players


def _analyses_rows(conn, game_id, as_of="", player_id="", limit=50):
    """历史分析（热 + 冷）按时间倒序取一批，带关联标的。"""
    parts, args = [], []
    for table in ("analyses", "analyses_cold"):
        sql = ("SELECT id,player_id,date,kind,text,keywords,created_at FROM %s "
               "WHERE game_id=?" % table)
        a = [game_id]
        if as_of:
            sql += " AND date<=?"
            a.append(as_of)
        if player_id:
            sql += " AND player_id=?"
            a.append(player_id)
        parts.append(sql)
        args += a
    union = " UNION ALL ".join(parts)
    rows = conn.execute(
        "SELECT * FROM (%s) ORDER BY date DESC, created_at DESC LIMIT ?" % union,
        tuple(args + [max(1, min(int(limit or 50), 500))])).fetchall()
    items = [_row(r) for r in rows]
    sym_map = _symbols_of_analyses(conn, [x["id"] for x in items])
    for x in items:
        x["symbols"] = sym_map.get(x["id"], [])
    return items


def timeline_dates(game_id):
    """可回溯的模拟交易日列表（从开局日到当前模拟日，不包含未来）。"""
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        rows = conn.execute(
            "SELECT DISTINCT date FROM bars WHERE game_id=? AND symbol=? "
            "AND date>=? AND date<=? ORDER BY date",
            (game_id, _bm(game), game["start_date"], game["cursor_date"])).fetchall()
        return [r["date"] for r in rows]


def state_at(game_id, date="", symbol="", limit=120):
    """把对局完整还原到某个历史时间点（只读）。

    行情 / 持仓 / 资产 / AI 分析 / 成交全部按 date<=T 重放，不读任何未来数据。
    """
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        as_of = _clamp_date(game, date)
        sym = ""
        if symbol:
            try:
                sym = normalize_symbol(symbol)
            except StockError:
                sym = ""
        replay = _replay_positions(conn, game_id, as_of)
        closes = _last_closes(conn, game_id, as_of)
        decided = _decided_set(conn, game_id, as_of)
        pviews = []
        for p in _players(conn, game_id):
            st = replay.get(p["id"]) or {"cash": 0.0, "realized_pnl": 0.0, "positions": {}}
            cash = float(st["cash"])
            mv = 0.0
            pos_out = []
            for pos in st["positions"].values():
                qty = _qnum(pos["qty"])
                last = closes.get(pos["symbol"], 0.0)
                value = qty * last
                mv += value
                avg = float(pos["avg_cost"] or 0)
                pos_out.append({
                    "symbol": pos["symbol"], "qty": qty,
                    "available_qty": _qnum(pos["available_qty"]),
                    "avg_cost": round(avg, 3), "last": round(last, 3),
                    "market_value": round(value, 2),
                    "pnl_pct": round((last / avg - 1.0) * 100, 2) if avg > 0 else 0.0,
                })
            equity = cash + mv
            init = float(p["initial_cash"]) or 1.0
            pviews.append({
                "id": p["id"], "name": p["name"], "kind": p["kind"],
                "persona": p["persona"],
                "cash": round(cash, 2), "market_value": round(mv, 2),
                "equity": round(equity, 2),
                "return_pct": round((equity / init - 1.0) * 100, 2),
                "realized_pnl": round(float(st["realized_pnl"] or 0), 2),
                "positions": pos_out, "decided": p["id"] in decided,
            })
        human = next((p for p in pviews if p["kind"] == "human"), None)
        analyses = _analyses_rows(conn, game_id, as_of=as_of, limit=60)
        analyses_total = int(conn.execute(
            "SELECT (SELECT COUNT(*) FROM analyses WHERE game_id=? AND date<=?) + "
            "(SELECT COUNT(*) FROM analyses_cold WHERE game_id=? AND date<=?) AS n",
            (game_id, as_of, game_id, as_of)).fetchone()["n"])
        syms = _symbols_view(conn, game_id, as_of)
        if not sym and syms:
            sym = syms[0]["symbol"]
        chart = {"as_of": as_of, "symbol": sym, "bars": [], "marks": []}
        if sym:
            rows = conn.execute(
                "SELECT date,open,high,low,close,volume FROM bars "
                "WHERE game_id=? AND symbol=? AND date<=? ORDER BY date DESC LIMIT ?",
                (game_id, sym, as_of, max(10, min(int(limit or 120), 240)))).fetchall()
            chart["bars"] = [dict(r) for r in reversed(rows)]
            chart["marks"] = _trades_of(conn, game_id, symbol=sym, date_to=as_of, limit=400)
        snap = None
        if human:
            snap = _row(conn.execute(
                "SELECT * FROM snapshots WHERE game_id=? AND player_id=? AND date=?",
                (game_id, human["id"], as_of)).fetchone())
        return {
            "id": game_id, "name": game["name"], "mode": game["mode"],
            "as_of": as_of, "readonly": True,
            "start_date": game["start_date"], "cursor_date": game["cursor_date"],
            "next_date": _next_date(conn, game),
            "symbols": syms, "players": pviews,
            "leaderboard": sorted(pviews, key=lambda x: x["equity"], reverse=True),
            "analyses": analyses,
            "journals": _journals_map(conn, game_id, as_of, limit=5),
            "trades": _trades_of(conn, game_id, date_to=as_of, limit=50),
            "orders": [_row(r) for r in conn.execute(
                "SELECT o.*, s.name FROM orders o LEFT JOIN symbols s "
                "ON s.game_id=o.game_id AND s.symbol=o.symbol "
                "WHERE o.game_id=? AND o.created_date<=? "
                "ORDER BY o.created_date DESC, o.created_at DESC LIMIT 50",
                (game_id, as_of)).fetchall()],
            "benchmark": _benchmark_stats(conn, game, as_of),
            "chart": chart,
            "snapshot": snap,
            "analyses_total": analyses_total,
        }


def compare_states(game_id, dates, symbol="", limit=120):
    """最多 3 个时间点并列对比：每个节点都是完整只读快照。"""
    ds = [str(d or "")[:10] for d in (dates or []) if str(d or "").strip()]
    if len(ds) < 2:
        raise StockError("至少要选择 2 个时间点做对比")
    if len(ds) > 3:
        raise StockError("最多同时对比 3 个时间点")
    nodes = [state_at(game_id, d, symbol=symbol, limit=limit) for d in ds]
    return {"nodes": nodes, "count": len(nodes)}


# ============================================================
# 训练辅助：市场状态 / 盲预测 / 决策计分板 / 参考散户
# ============================================================

PREDICTION_HORIZONS = (1, 3, 5)
PREDICTION_DIRECTIONS = ("up", "down", "flat")
TARGET_BENCHMARK = "benchmark"

_STYLE_LABELS = {
    "ai_value": "价值", "ai_trend": "趋势", "ai_contrarian": "逆势",
}


def _trading_dates(conn, game_id, after, until="", limit=0, benchmark=""):
    """交易日历：after（不含）到 until（含）之间的交易日。"""
    sym = benchmark or _bm(_game(conn, game_id))
    sql = "SELECT date FROM bars WHERE game_id=? AND symbol=? AND date>?"
    args = [game_id, sym, after]
    if until:
        sql += " AND date<=?"
        args.append(until)
    sql += " ORDER BY date"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [r["date"] for r in conn.execute(sql, tuple(args)).fetchall()]


def _closes(conn, game_id, symbol, until, limit):
    rows = conn.execute(
        "SELECT close FROM bars WHERE game_id=? AND symbol=? AND date<=? "
        "ORDER BY date DESC LIMIT ?", (game_id, symbol, until, limit)).fetchall()
    return [float(r["close"] or 0) for r in reversed(rows)]


def _regime(conn, game_id, date):
    """用本局基准（沪深300 / BTC）判断「当前是什么行情」：趋势 / 波动 / 量能。

    阈值不用绝对值而用相对分位，避免不同标的、不同时期尺度不一致。
    """
    rows = conn.execute(
        "SELECT close,volume FROM bars WHERE game_id=? AND symbol=? AND date<=? "
        "ORDER BY date DESC LIMIT 130", (game_id, _bm(_game(conn, game_id)), date)).fetchall()
    rows = list(reversed(rows))
    neutral = {"trend": "range", "trend_label": "震荡", "vol": "mid",
               "vol_label": "中波动", "volume": "normal", "volume_label": "正常",
               "label": "数据不足", "vol20": 0.0, "volume_ratio": 0.0,
               "close": 0.0, "ma20": 0.0}
    if len(rows) < 21:
        return neutral
    closes = [float(r["close"] or 0) for r in rows]
    vols = [float(r["volume"] or 0) for r in rows]

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    ma20 = mean(closes[-20:])
    ma20_prev = mean(closes[-25:-5]) if len(closes) >= 25 else ma20
    slope = (ma20 / ma20_prev - 1.0) if ma20_prev else 0.0
    last = closes[-1]
    if last > ma20 and slope > 0.003:
        trend, trend_label = "up", "趋势↑"
    elif last < ma20 and slope < -0.003:
        trend, trend_label = "down", "趋势↓"
    else:
        trend, trend_label = "range", "震荡"

    def stdev_of(seg):
        rets = [seg[i] / seg[i - 1] - 1.0 for i in range(1, len(seg)) if seg[i - 1]]
        return statistics.pstdev(rets) if len(rets) >= 2 else 0.0

    vol20 = stdev_of(closes[-21:])
    hist = [stdev_of(closes[end - 20:end])
            for end in range(max(20, len(closes) - 100), len(closes) + 1)]
    med = statistics.median([x for x in hist if x > 0]) if any(x > 0 for x in hist) else vol20
    if med > 0 and vol20 > med * 1.3:
        vol, vol_label = "high", "高波动"
    elif med > 0 and vol20 < med * 0.75:
        vol, vol_label = "low", "低波动"
    else:
        vol, vol_label = "mid", "中波动"

    base_vol = mean(vols[-21:-1])
    ratio = (vols[-1] / base_vol) if base_vol > 0 else 0.0
    if ratio > 1.3:
        vstate, vlabel = "heavy", "放量"
    elif 0 < ratio < 0.8:
        vstate, vlabel = "light", "缩量"
    else:
        vstate, vlabel = "normal", "正常"

    return {"trend": trend, "trend_label": trend_label, "vol": vol,
            "vol_label": vol_label, "volume": vstate, "volume_label": vlabel,
            "label": "%s · %s · %s" % (trend_label, vol_label, vlabel),
            "vol20": round(vol20 * 100, 3), "volume_ratio": round(ratio, 2),
            "close": round(last, 2), "ma20": round(ma20, 2)}


def market_regime(game_id, date=""):
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        return _regime(conn, game_id, str(date or "")[:10] or game["cursor_date"])


def _target_key(raw):
    t = str(raw or "").strip().lower()
    if t in ("benchmark", "hs300", "sh000300", "沪深300", "大盘"):
        return TARGET_BENCHMARK
    return normalize_symbol(raw)


def _target_name(conn, game_id, target):
    if target == TARGET_BENCHMARK:
        return _bm_name(_game(conn, game_id))
    r = conn.execute("SELECT name FROM symbols WHERE game_id=? AND symbol=?",
                     (game_id, target)).fetchone()
    return r["name"] if r else target


def _target_symbol(target, benchmark=BENCHMARK_SYMBOL):
    """预测里用 'benchmark' 作为目标键，取行情时要换成该局的真实基准。"""
    return benchmark if target == TARGET_BENCHMARK else target


def _prediction_threshold(conn, game_id, target, date, horizon):
    """自适应阈值（百分数）：按该标的近 20 日波动放大到 N 日，至少 0.5%。

    不这样就会出现「涨 0.1% 也算看对」，训练就没有意义。
    """
    closes = _closes(conn, game_id, target, date, 21)
    rets = [closes[i] / closes[i - 1] - 1.0
            for i in range(1, len(closes)) if closes[i - 1]]
    sigma = statistics.pstdev(rets) if len(rets) >= 2 else 0.0
    return round(max(0.5, sigma * math.sqrt(horizon) * 0.6 * 100), 2)


def _prediction_row(conn, game_id, player_id, target, created_date, horizon):
    row = conn.execute(
        "SELECT * FROM predictions WHERE game_id=? AND player_id=? AND target=? "
        "AND created_date=? AND horizon=?",
        (game_id, player_id, target, created_date, horizon)).fetchone()
    return _row(row)


def _decorate_prediction(conn, game_id, cursor, p):
    p["target_name"] = _target_name(conn, game_id, p["target"])
    p["settled"] = int(p["hit"]) >= 0
    if p["settled"]:
        p["result_label"] = {1: "对", 0: "错"}.get(int(p["hit"]), "")
        p["remaining_days"] = 0
    else:
        done = len(_trading_dates(conn, game_id, p["created_date"], cursor))
        p["remaining_days"] = max(0, int(p["horizon"]) - done)
    p["direction_label"] = {"up": "看涨", "down": "看跌", "flat": "看平"}.get(
        p["direction"], p["direction"])
    return p


def create_prediction(game_id, player_id="you", target="", horizon=3, direction="up"):
    """下注：某个标的 N 个交易日后的方向。到期由 _settle_predictions 自动结算。"""
    init_db()
    with _GAME_LOCK:
        conn = get_db()
        game = _game(conn, game_id)
        if game["status"] != "running":
            raise StockError("这一局已经结束了")
        _player(conn, game_id, player_id)
        try:
            horizon = int(horizon)
        except (TypeError, ValueError):
            raise StockError("期限必须是 1 / 3 / 5 个交易日")
        if horizon not in PREDICTION_HORIZONS:
            raise StockError("期限必须是 1 / 3 / 5 个交易日")
        d = str(direction or "").strip().lower()
        if d not in PREDICTION_DIRECTIONS:
            raise StockError("方向只能是 看涨 / 看平 / 看跌")
        tgt = _target_key(target)
        if tgt != TARGET_BENCHMARK and not _is_tradable(conn, game_id, tgt):
            raise StockError("这只标的不在本局观察池里")
        cursor = game["cursor_date"]
        base = _last_close(conn, game_id, _target_symbol(tgt, _bm(game)), cursor)
        if base <= 0:
            raise StockError("没有可用行情，暂时不能预测")
        if _prediction_row(conn, game_id, player_id, tgt, cursor, horizon):
            raise StockError("今天已经对这只标的的这个期限下过注了")
        future = _trading_dates(conn, game_id, cursor, game["end_date"] or "", horizon)
        if game["mode"] == "replay" and len(future) < horizon:
            raise StockError("剩余交易日不足，无法验证 %d 日预测" % horizon)
        due = future[horizon - 1] if len(future) >= horizon else ""
        threshold = _prediction_threshold(conn, game_id, _target_symbol(tgt, _bm(game)),
                                          cursor, horizon)
        pid = "pp_" + uuid.uuid4().hex[:10]
        conn.execute(
            "INSERT INTO predictions(id,game_id,player_id,created_date,target,horizon,"
            "direction,base_close,threshold,due_date,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, game_id, player_id, cursor, tgt, horizon, d, base, threshold,
             due, time.time()))
        conn.commit()
        row = _prediction_row(conn, game_id, player_id, tgt, cursor, horizon)
        return _decorate_prediction(conn, game_id, cursor, row)


def list_predictions(game_id, player_id="you", limit=60):
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM predictions WHERE game_id=? AND player_id=? "
            "ORDER BY created_date DESC, created_at DESC LIMIT ?",
            (game_id, player_id, max(1, min(int(limit or 60), 300)))).fetchall()]
        return [_decorate_prediction(conn, game_id, game["cursor_date"], p) for p in rows]


def _settle_predictions(conn, game_id, cursor):
    """把到期预测结算成 对 / 错。只用到 <= cursor 的行情，不引入未来数据。"""
    rows = [_row(r) for r in conn.execute(
        "SELECT * FROM predictions WHERE game_id=? AND hit=-1 AND created_date<?",
        (game_id, cursor)).fetchall()]
    for p in rows:
        h = int(p["horizon"])
        dates = _trading_dates(conn, game_id, p["created_date"], cursor)
        if len(dates) < h:
            continue
        due = dates[h - 1]
        close = _last_close(conn, game_id, _target_symbol(p["target"], _bm(_game(conn, game_id))), due)
        base = float(p["base_close"] or 0)
        if close <= 0 or base <= 0:
            continue
        actual = (close / base - 1.0) * 100
        thr = float(p["threshold"] or 0)
        if actual > thr:
            got = "up"
        elif actual < -thr:
            got = "down"
        else:
            got = "flat"
        conn.execute(
            "UPDATE predictions SET due_date=?,settle_close=?,actual_pct=?,hit=?,settled_at=? "
            "WHERE id=?",
            (due, close, round(actual, 3), 1 if got == p["direction"] else 0,
             time.time(), p["id"]))


def prediction_stats(game_id, player_id="you"):
    """命中率：总体 / 按期限 / 按标的 / 按市场状态，以及当前连胜连败。"""
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        _game(conn, game_id)              # 校验对局存在
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM predictions WHERE game_id=? AND player_id=? "
            "ORDER BY created_date, created_at", (game_id, player_id)).fetchall()]
        settled = [r for r in rows if int(r["hit"]) >= 0]
        pending = [r for r in rows if int(r["hit"]) < 0]

        def bucket(items):
            n = len(items)
            hits = sum(1 for x in items if int(x["hit"]) == 1)
            return {"settled": n, "hits": hits,
                    "accuracy": round(hits / n * 100, 1) if n else 0.0}

        by_horizon = []
        for h in PREDICTION_HORIZONS:
            items = [x for x in settled if int(x["horizon"]) == h]
            if items:
                by_horizon.append(dict(bucket(items), horizon=h))

        by_target = []
        names = {}
        for x in settled:
            tgt = x["target"]
            if tgt not in names:
                names[tgt] = _target_name(conn, game_id, tgt)
            item = next((b for b in by_target if b["target"] == tgt), None)
            if item is None:
                item = {"target": tgt, "name": names[tgt], "settled": 0, "hits": 0}
                by_target.append(item)
            item["settled"] += 1
            item["hits"] += 1 if int(x["hit"]) == 1 else 0
        for b in by_target:
            b["accuracy"] = round(b["hits"] / b["settled"] * 100, 1) if b["settled"] else 0.0

        cache = {}
        groups = {}
        for x in settled:
            d = x["created_date"]
            if d not in cache:
                cache[d] = _regime(conn, game_id, d)
            rg = cache[d]
            key = (rg["trend"], rg["vol"], rg["volume"])
            g = groups.get(key)
            if g is None:
                g = {"trend": rg["trend"], "vol": rg["vol"], "volume": rg["volume"],
                     "label": rg["label"], "settled": 0, "hits": 0}
                groups[key] = g
            g["settled"] += 1
            g["hits"] += 1 if int(x["hit"]) == 1 else 0
        by_regime = []
        for g in groups.values():
            g["accuracy"] = round(g["hits"] / g["settled"] * 100, 1) if g["settled"] else 0.0
            by_regime.append(g)
        by_regime.sort(key=lambda g: (-g["settled"], -g["accuracy"]))

        streak = {"type": "", "n": 0}
        if settled:
            last = int(settled[-1]["hit"])
            n = 0
            for x in reversed(settled):
                if int(x["hit"]) != last:
                    break
                n += 1
            streak = {"type": "hit" if last == 1 else "miss", "n": n}

        base = bucket(settled)
        return {"total": len(rows), "settled": base["settled"], "hits": base["hits"],
                "accuracy": base["accuracy"], "pending": len(pending),
                "streak": streak, "by_horizon": by_horizon,
                "by_target": by_target, "by_regime": by_regime}


def _fwd_pct(conn, game_id, symbol, date, n, cursor):
    """成交后第 n 个交易日的涨跌幅（百分数）；数据不足返回 None。"""
    dates = _trading_dates(conn, game_id, date, cursor, n)
    if len(dates) < n:
        return None
    due = dates[n - 1]
    c0 = _last_close(conn, game_id, symbol, date)
    c1 = _last_close(conn, game_id, symbol, due)
    if c0 <= 0 or c1 <= 0:
        return None
    return round((c1 / c0 - 1.0) * 100, 3)


def trade_scoreboard(game_id, player_id="you", limit=100):
    """决策计分板：衡量「判断对不对」，而不是账户一时的盈亏。

    买入看后续几天是否上涨；卖出看后续是否下跌（躲过下跌即算判断正确）。
    """
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        cursor = game["cursor_date"]
        rows = [_row(r) for r in conn.execute(
            "SELECT t.*, s.name FROM trades t LEFT JOIN symbols s "
            "ON s.game_id=t.game_id AND s.symbol=t.symbol "
            "WHERE t.game_id=? AND t.player_id=? "
            "ORDER BY t.date ASC, t.created_at ASC", (game_id, player_id)).fetchall()]
        rows = rows[-max(1, min(int(limit or 100), 500)):]
        out = []
        for t in rows:
            fwd = {}
            for n in (1, 3, 5, 10):
                fwd[n] = _fwd_pct(conn, game_id, t["symbol"], t["date"], n, cursor)
            bench5 = _fwd_pct(conn, game_id, game["benchmark"], t["date"], 5, cursor)
            excess5 = (round(fwd[5] - bench5, 3)
                       if fwd[5] is not None and bench5 is not None else None)
            out.append({"id": t["id"], "date": t["date"], "symbol": t["symbol"],
                        "name": t["name"] or t["symbol"], "side": t["side"],
                        "qty": _qnum(t["qty"]), "price": round(float(t["price"] or 0), 3),
                        "fwd1": fwd[1], "fwd3": fwd[3], "fwd5": fwd[5],
                        "fwd10": fwd[10], "excess5": excess5})
        buys = [x for x in out if x["side"] == "buy" and x["fwd5"] is not None]
        sells = [x for x in out if x["side"] == "sell" and x["fwd5"] is not None]
        buy_win = sum(1 for x in buys if x["fwd5"] > 0)
        buy_ex = [x["excess5"] for x in buys if x["excess5"] is not None]
        sell_avoid = sum(1 for x in sells if x["fwd5"] < 0)
        return {
            "trades": out,
            "stats": {
                "buy_count": len(buys),
                "buy_win5": buy_win,
                "buy_win5_pct": round(buy_win / len(buys) * 100, 1) if buys else 0.0,
                "buy_avg_fwd5": round(sum(x["fwd5"] for x in buys) / len(buys), 2)
                if buys else 0.0,
                "buy_avg_excess5": round(sum(buy_ex) / len(buy_ex), 2) if buy_ex else 0.0,
                "sell_count": len(sells),
                "sell_avoid5": sell_avoid,
                "sell_avoid5_pct": round(sell_avoid / len(sells) * 100, 1) if sells else 0.0,
                "sell_avg_fwd5": round(sum(x["fwd5"] for x in sells) / len(sells), 2)
                if sells else 0.0,
            },
        }


def ai_reference(game_id):
    """参考散户：四个参与者的净值曲线 + 行为指标（仓位 / 换手 / 集中度 / 回撤）。"""
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        cursor = game["cursor_date"]
        players = _players(conn, game_id)
        snaps = conn.execute(
            "SELECT player_id,date,equity FROM snapshots WHERE game_id=? AND date<=? "
            "ORDER BY date", (game_id, cursor)).fetchall()
        dates = sorted({r["date"] for r in snaps})
        series = {}
        for r in snaps:
            series.setdefault(r["player_id"], {})[r["date"]] = float(r["equity"] or 0)
        turnover = {r["player_id"]: float(r["sum_amount"] or 0)
                    for r in conn.execute(
                        "SELECT player_id, SUM(amount) AS sum_amount FROM trades "
                        "WHERE game_id=? GROUP BY player_id", (game_id,)).fetchall()}
        views = {p["id"]: p for p in _players_view(conn, game, cursor)}
        names = {r["symbol"]: r["name"] for r in conn.execute(
            "SELECT symbol,name FROM symbols WHERE game_id=?", (game_id,)).fetchall()}

        out = []
        for p in players:
            init_cash = float(p["initial_cash"]) or 1.0
            raw = series.get(p["id"], {})
            nav = []
            last = 1.0
            for d in dates:
                if d in raw:
                    last = raw[d] / init_cash
                nav.append(round(last, 5))
            peak = -1e9
            max_dd = 0.0
            for v in nav:
                peak = max(peak, v)
                if peak > 0:
                    max_dd = min(max_dd, (v / peak - 1.0) * 100)
            recent5 = (round((nav[-1] / nav[-6] - 1.0) * 100, 2)
                       if len(nav) >= 6 else 0.0)
            view = views.get(p["id"]) or {}
            positions = view.get("positions") or []
            mv = sum(float(x.get("market_value") or 0) for x in positions)
            top3 = sum(sorted((float(x.get("market_value") or 0) for x in positions),
                              reverse=True)[:3])
            equity = float(view.get("equity") or 0)
            out.append({
                "id": p["id"], "name": p["name"], "kind": p["kind"],
                "style": _STYLE_LABELS.get(p["persona"] or p["id"],
                                           "你" if p["kind"] == "human" else "参考"),
                "nav": nav,
                "total_return": round((nav[-1] - 1.0) * 100, 2) if nav else 0.0,
                "recent5": recent5,
                "position_pct": round(mv / equity * 100, 1) if equity > 0 else 0.0,
                "turnover": round(turnover.get(p["id"], 0.0) / init_cash * 100, 1),
                "concentration": round(top3 / mv * 100, 1) if mv > 0 else 0.0,
                "realized_pnl": round(float(view.get("realized_pnl") or 0), 2),
                "max_drawdown": round(max_dd, 2),
                "equity": round(equity, 2),
                "positions": [{"symbol": x.get("symbol"),
                               "name": names.get(x.get("symbol"), x.get("symbol")),
                               "qty": _qnum(x.get("qty") or 0),
                               "market_value": round(float(x.get("market_value") or 0), 2)}
                              for x in positions],
            })
        # 曲线太长就只留最近 120 个点
        if len(dates) > 120:
            dates = dates[-120:]
            for p in out:
                p["nav"] = p["nav"][-120:]
        return {"dates": dates, "players": out}


def analysis_bundle(game_id, player_id="you"):
    """一次取齐训练辅助数据，减少前端往返。"""
    return {
        "regime": market_regime(game_id),
        "predictions": list_predictions(game_id, player_id),
        "prediction_stats": prediction_stats(game_id, player_id),
        "scoreboard": trade_scoreboard(game_id, player_id),
        "reference": ai_reference(game_id),
    }


def audit_game(game_id, tol=0.02):
    """一致性自检：把库里的现金/持仓/快照与「按成交重放」的账逐人对平。"""
    init_db()
    conn = get_db()
    with _GAME_LOCK:
        game = _game(conn, game_id)
        cursor = game["cursor_date"]
        replay = _replay_positions(conn, game_id, cursor)
        closes = _last_closes(conn, game_id, cursor)
        problems = []
        for p in _players(conn, game_id):
            st = replay.get(p["id"]) or {"cash": 0.0, "positions": {}}
            if abs(float(p["cash"]) - float(st["cash"])) > tol:
                problems.append({"kind": "cash", "player_id": p["id"],
                                 "db": round(float(p["cash"]), 2),
                                 "replay": round(float(st["cash"]), 2)})
            db_pos = {r["symbol"]: float(r["qty"]) for r in _positions(conn, game_id, p["id"])}
            rp_pos = {s: float(x["qty"]) for s, x in st["positions"].items()}
            for sym in sorted(set(db_pos) | set(rp_pos)):
                if db_pos.get(sym, 0) != rp_pos.get(sym, 0):
                    problems.append({"kind": "qty", "player_id": p["id"], "symbol": sym,
                                     "db": db_pos.get(sym, 0), "replay": rp_pos.get(sym, 0)})
            snap = _row(conn.execute(
                "SELECT * FROM snapshots WHERE game_id=? AND player_id=? AND date=?",
                (game_id, p["id"], cursor)).fetchone())
            if snap:
                mv = sum(float(x["qty"]) * closes.get(s, 0.0)
                         for s, x in st["positions"].items())
                if abs(float(snap["market_value"]) - mv) > tol:
                    problems.append({"kind": "snapshot_market_value", "player_id": p["id"],
                                     "db": round(float(snap["market_value"]), 2),
                                     "replay": round(mv, 2)})
        return problems


# ============================================================
# 下单 / 撤单
# ============================================================

def _pending_buy_cost(conn, game, player_id):
    """未成交买单占用的资金：连表一次拿齐现价（原来每笔委托一次查询）。"""
    total = 0.0
    for r in conn.execute(
            "SELECT o.qty, "
            "(SELECT b.close FROM bars b WHERE b.game_id=o.game_id AND b.symbol=o.symbol "
            " AND b.date<=? ORDER BY b.date DESC LIMIT 1) AS px "
            "FROM orders o WHERE o.game_id=? AND o.player_id=? AND o.status='pending' "
            "AND o.side='buy'",
            (game["cursor_date"], game["id"], player_id)).fetchall():
        amount = float(r["px"] or 0) * float(r["qty"])
        total += amount + calc_fee("buy", amount)
    return total


def _pending_qty(conn, game_id, player_id, symbol, side):
    r = conn.execute(
        "SELECT COALESCE(SUM(qty),0) AS n FROM orders WHERE game_id=? AND player_id=? "
        "AND symbol=? AND side=? AND status='pending'",
        (game_id, player_id, symbol, side)).fetchone()
    return int((r["n"] if r else 0) or 0)


def place_order(game_id, player_id="you", symbol="", side="buy", qty=0, reason=""):
    init_db()
    with _GAME_LOCK:
        conn = get_db()
        game = _game(conn, game_id)
        if game["status"] != "running":
            raise StockError("这一局已经结束了")
        p = _player(conn, game_id, player_id)
        s = normalize_symbol(symbol)
        crypto = is_crypto(s)
        if not _is_tradable(conn, game_id, s):
            raise StockError("这只标的不能交易")
        if crypto:
            try:
                qty = round(float(qty), CRYPTO_QTY_DECIMALS)
            except (TypeError, ValueError):
                raise StockError("数量必须是数字")
            if qty <= 0:
                raise StockError("数量必须大于 0")
        else:
            try:
                qty = int(float(qty))
            except (TypeError, ValueError):
                raise StockError("数量必须是整数")
            if qty <= 0:
                raise StockError("数量必须大于 0")
            if qty % lot_size(s) != 0:
                raise StockError("数量必须是 %d 的整数倍" % lot_size(s))
        side = "sell" if str(side).lower() in ("sell", "卖出", "s") else "buy"
        if side == "sell":
            pos = _position(conn, game_id, player_id, s)
            # 币市 7×24、无 T+1；股票仍按可卖数量（当天买入不可卖）
            available = (float(pos["qty"]) if pos else 0.0) if crypto \
                else (float(pos["available_qty"]) if pos else 0.0)
            pending_sell = float(_pending_qty(conn, game_id, player_id, s, "sell") or 0)
            if available - pending_sell < qty - 1e-9:
                raise StockError("可卖数量不足" if crypto
                                 else "可卖数量不足（T+1：今天买入的份额明天才能卖）")
        else:
            px = _last_close(conn, game_id, s, game["cursor_date"])
            if px <= 0:
                raise StockError("没有可用行情，暂时不能买入")
            amount = px * qty
            cost = amount + calc_fee("buy", amount)
            left = float(p["cash"]) - _pending_buy_cost(conn, game, player_id)
            if left + 1e-6 < cost:
                raise StockError("现金不足：这笔预计需要 %.2f %s，可用约 %.2f %s"
                                 % (cost, "USDT" if crypto else "元",
                                    max(0.0, left), "USDT" if crypto else "元"))
            # 单票仓位上限（按当前价估算）：持仓 + 未成交买单一起算，
            # 否则可以拆成多笔绕过 35% 上限。持仓估值一次取回，不再逐只查询。
            # 币市不设单币上限，避免 3 个币种 + 高波动下无法建仓。
            if not crypto:
                cur = 0.0
                equity = float(p["cash"])
                for pos in _positions_with_last(conn, game_id, player_id, game["cursor_date"]):
                    value = float(pos["qty"]) * float(pos["last"] or 0)
                    equity += value
                    if pos["symbol"] == s:
                        cur = value
                cur += px * _pending_qty(conn, game_id, player_id, s, "buy")
                if equity > 0 and (cur + amount) > equity * MAX_SINGLE_POSITION + 1e-6:
                    raise StockError("单只标的不超过总资产的 %d%%"
                                     % int(MAX_SINGLE_POSITION * 100))
        oid = "so_" + uuid.uuid4().hex[:10]
        now = time.time()
        name = conn.execute(
            "SELECT name FROM symbols WHERE game_id=? AND symbol=?",
            (game_id, s)).fetchone()
        conn.execute(
            "INSERT INTO orders(id,game_id,player_id,created_date,symbol,side,qty,status,"
            "reason,reject,fill_date,fill_price,fee,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, game_id, player_id, game["cursor_date"], s, side, qty, "pending",
             str(reason or "")[:200], "", "", 0, 0, now))
        conn.commit()
        out = _row(conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone())
        out["name"] = name["name"] if name else s
        return out


def cancel_order(game_id, order_id, player_id="you"):
    init_db()
    with _GAME_LOCK:
        conn = get_db()
        _game(conn, game_id)
        o = _row(conn.execute(
            "SELECT * FROM orders WHERE id=? AND game_id=?", (order_id, game_id)).fetchone())
        if not o or o["player_id"] != player_id:
            raise StockError("找不到这笔委托")
        if o["status"] != "pending":
            raise StockError("这笔委托已经成交或已撤销")
        conn.execute("DELETE FROM orders WHERE id=?", (order_id,))
        conn.commit()
    return {"ok": True}


# ============================================================
# 推进 / 撮合
# ============================================================

def _reject(conn, order, reason):
    conn.execute("UPDATE orders SET status='rejected', reject=? WHERE id=?",
                 (str(reason)[:160], order["id"]))


def _execute_pending(conn, game, date):
    """在 date 开盘撮合所有 pending 委托，随后按 date 收盘估值。

    批量化：当日 bar、前收盘、标的名、参与者、持仓全部预取一次，
    撮合循环里只做内存查表 —— 委托越多，省下的查询越多。
    """
    gid = game["id"]
    # T+1：新的一天开始，昨天及以前买入的份额变为可卖
    conn.execute("UPDATE positions SET available_qty=qty WHERE game_id=?", (gid,))
    orders = [_row(r) for r in conn.execute(
        "SELECT * FROM orders WHERE game_id=? AND status='pending' ORDER BY created_at",
        (gid,)).fetchall()]
    if orders:
        bars = {r["symbol"]: _row(r) for r in conn.execute(
            "SELECT * FROM bars WHERE game_id=? AND date=?", (gid, date)).fetchall()}
        prevs = {r["symbol"]: float(r["close"] or 0) for r in conn.execute(
            "SELECT b.symbol, b.close FROM bars b JOIN ("
            " SELECT symbol, MAX(date) AS d FROM bars WHERE game_id=? AND date<? "
            " GROUP BY symbol) m ON m.symbol=b.symbol AND m.d=b.date "
            "WHERE b.game_id=?", (gid, date, gid)).fetchall()}
        names = {r["symbol"]: r["name"] for r in conn.execute(
            "SELECT symbol,name FROM symbols WHERE game_id=?", (gid,)).fetchall()}
        players = {r["id"]: _row(r) for r in conn.execute(
            "SELECT * FROM players WHERE game_id=?", (gid,)).fetchall()}
        positions = {(r["player_id"], r["symbol"]): _row(r) for r in conn.execute(
            "SELECT * FROM positions WHERE game_id=?", (gid,)).fetchall()}
        slippage = float(game["slippage"])
        fees = {"commission": game["commission"], "min_commission": game["min_commission"],
                "stamp_duty": game["stamp_duty"], "transfer_fee": game["transfer_fee"]}
        for o in orders:
            pid, sym = o["player_id"], o["symbol"]
            bar = bars.get(sym)
            if (not bar or float(bar["volume"] or 0) <= 0
                    or float(bar["open"] or 0) <= 0):
                _reject(conn, o, "停牌或没有当日行情")
                continue
            pc = prevs.get(sym, 0.0)
            lim = limit_pct(sym, names.get(sym) or "")
            # 币市 lim=0：没有涨跌停，跳过这组判断（否则 1+0 会把所有委托都判成涨停/跌停）
            if pc > 0 and lim > 0:
                if o["side"] == "buy" and float(bar["open"]) >= pc * (1 + lim) - 1e-6:
                    _reject(conn, o, "开盘涨停，买不进")
                    continue
                if o["side"] == "sell" and float(bar["open"]) <= pc * (1 - lim) + 1e-6:
                    _reject(conn, o, "开盘跌停，卖不出")
                    continue
            qty = float(o["qty"])
            if o["side"] == "buy":
                px = float(bar["open"]) * (1 + slippage)
            else:
                px = float(bar["open"]) * (1 - slippage)
            px = min(max(px, float(bar["low"])), float(bar["high"]))
            amount = px * qty
            fee = calc_fee(o["side"], amount, fees)
            p = players.get(pid) or _player(conn, gid, pid)
            pos = positions.get((pid, sym))
            if o["side"] == "buy":
                cost = amount + fee
                if float(p["cash"]) + 1e-6 < cost:
                    _reject(conn, o, "现金不足")
                    continue
                old_qty = float(pos["qty"]) if pos else 0.0
                old_avg = float(pos["avg_cost"]) if pos else 0.0
                new_qty = old_qty + qty
                if is_crypto(sym):
                    new_qty = round(new_qty, CRYPTO_QTY_DECIMALS)
                new_avg = ((old_qty * old_avg) + amount + fee) / new_qty if new_qty else 0.0
                p["cash"] = float(p["cash"]) - cost
                conn.execute("UPDATE players SET cash=cash-? WHERE game_id=? AND id=?",
                             (cost, gid, pid))
                avail = float(pos["available_qty"]) if pos else 0.0
                if is_crypto(sym):        # 币市无 T+1：买入即可卖
                    avail = new_qty
                positions[(pid, sym)] = {"player_id": pid, "symbol": sym, "qty": new_qty,
                                         "available_qty": avail, "avg_cost": new_avg}
                conn.execute(
                    "INSERT OR REPLACE INTO positions(game_id,player_id,symbol,qty,"
                    "available_qty,avg_cost) VALUES(?,?,?,?,?,?)",
                    (gid, pid, sym, new_qty, avail, new_avg))
                realized = 0.0
            else:
                if not pos or float(pos["available_qty"]) + 1e-9 < qty:
                    _reject(conn, o, "可卖数量不足")
                    continue
                proceeds = amount - fee
                avg = float(pos["avg_cost"] or 0)
                realized = (px - avg) * qty - fee
                left = float(pos["qty"]) - qty
                if is_crypto(sym):
                    left = round(left, CRYPTO_QTY_DECIMALS)
                p["cash"] = float(p["cash"]) + proceeds
                p["realized_pnl"] = float(p["realized_pnl"] or 0) + realized
                conn.execute(
                    "UPDATE players SET cash=cash+?, realized_pnl=realized_pnl+? "
                    "WHERE game_id=? AND id=?", (proceeds, realized, gid, pid))
                if left > 0:
                    pos["qty"] = left
                    pos["available_qty"] = float(pos["available_qty"]) - qty
                    conn.execute(
                        "UPDATE positions SET qty=?, available_qty=available_qty-? "
                        "WHERE game_id=? AND player_id=? AND symbol=?",
                        (left, qty, gid, pid, sym))
                else:
                    positions.pop((pid, sym), None)
                    conn.execute(
                        "DELETE FROM positions WHERE game_id=? AND player_id=? AND symbol=?",
                        (gid, pid, sym))
            conn.execute(
                "UPDATE orders SET status='filled', fill_date=?, fill_price=?, fee=? WHERE id=?",
                (date, px, fee, o["id"]))
            conn.execute(
                "INSERT INTO trades(id,game_id,player_id,symbol,side,date,qty,price,fee,"
                "amount,realized_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("tr_" + uuid.uuid4().hex[:10], gid, pid, sym,
                 o["side"], date, qty, px, fee, amount, realized, time.time()))
    _snapshot_all(conn, gid, date)


def _update_bars_live(conn, game, symbols, next_date, bars_by_symbol=None,
                      benchmark_bars=None):
    """实时盘推进前补拉行情；只写 date <= next_date 的数据。"""
    if benchmark_bars is None:
        bm = fetch_benchmark(count=80, symbol=_bm(game))
    else:
        bm = list(benchmark_bars)
    bm = [dict(x) for x in bm if x.get("date") and x["date"] <= next_date]
    if bm:
        conn.executemany(
            "INSERT OR REPLACE INTO bars(game_id,symbol,date,open,high,low,close,volume,amount) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(game["id"], _bm(game), b["date"], b["open"], b["high"], b["low"],
              b["close"], b["volume"], b.get("amount") or 0) for b in bm])
    for s in symbols:
        if bars_by_symbol is not None:
            arr = bars_by_symbol.get(s["symbol"]) or []
        else:
            try:
                arr = fetch_daily(s["symbol"], 20)
            except StockError:
                continue
        arr = [dict(x) for x in arr if x.get("date") and x["date"] <= next_date]
        if not arr:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO bars(game_id,symbol,date,open,high,low,close,volume,amount) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(game["id"], s["symbol"], b["date"], b["open"], b["high"], b["low"],
              b["close"], b["volume"], b.get("amount") or 0) for b in arr])


def next_day(game_id, bars_by_symbol=None, benchmark_bars=None):
    """推进一个交易日：开盘撮合 -> 收盘估值。"""
    init_db()
    with _GAME_LOCK:
        conn = get_db()
        game = _game(conn, game_id)
        if game["status"] != "running":
            raise StockError("这一局已经结束了")
        cursor = game["cursor_date"]
        if game["mode"] == "live":
            if benchmark_bars is None:
                bm = fetch_benchmark(count=80, symbol=_bm(game))
            else:
                bm = list(benchmark_bars)
            bm = [dict(x) for x in bm if x.get("date")]
            if not is_crypto(_bm(game)):
                bm = _drop_partial_today(bm)
            dates = sorted({b["date"] for b in bm if b["date"] > cursor})
            if not dates:
                return {"no_new_day": True,
                        "message": ("还没有新的收盘数据；币市每天 08:00（北京时间）收一根日线。"
                                    if is_crypto(_bm(game))
                                    else "还没有新的收盘数据；A 股收盘后（约 15:05）再试。"),
                        "game": game_state(game_id)}
            nxt = dates[0]
            _update_bars_live(conn, game, _symbols(conn, game_id, True), nxt,
                              bars_by_symbol=bars_by_symbol, benchmark_bars=bm)
        else:
            nxt = _next_date(conn, game)
            if not nxt:
                conn.execute("UPDATE games SET status='finished' WHERE id=?", (game_id,))
                conn.commit()
                return {"finished": True, "game": game_state(game_id)}
        _execute_pending(conn, game, nxt)
        conn.execute("UPDATE games SET cursor_date=? WHERE id=?", (nxt, game_id))
        _settle_predictions(conn, game_id, nxt)
        if game["mode"] == "replay":
            rest = conn.execute(
                "SELECT MIN(date) AS d FROM bars WHERE game_id=? AND symbol=? "
                "AND date>? AND date<=?",
                (game_id, _bm(game), nxt,
                 game["end_date"] or "9999-12-31")).fetchone()
            if not (rest and rest["d"]):
                conn.execute("UPDATE games SET status='finished' WHERE id=?", (game_id,))
                # 对局结束即把该局分析搬进冷表（冷热分离），检索侧透明
                if _archive_analyses_tx(conn, game_id):
                    conn.commit()
                    rebuild_analyses_fts(conn)
        conn.commit()
        # 反馈必须反映「本次结算的那一天」，所以状态在锁内读，避免并发时读到后面的日
        result = {"game": game_state(game_id)}
    return result


# ============================================================
# AI 决策
# ============================================================

def _fmt(v, n=2):
    try:
        return ("%." + str(n) + "f") % float(v)
    except (TypeError, ValueError):
        return "0.00"


def _indicator_text(conn, game, symbol):
    rows = [_row(r) for r in conn.execute(
        "SELECT date,close,volume FROM bars WHERE game_id=? AND symbol=? AND date<=? "
        "ORDER BY date DESC LIMIT 60", (game["id"], symbol, game["cursor_date"])).fetchall()]
    rows.reverse()
    closes = [float(r["close"]) for r in rows]
    vols = [float(r["volume"] or 0) for r in rows]
    if not closes:
        return "无行情"
    last = closes[-1]

    def ma(n):
        return sum(closes[-n:]) / min(n, len(closes))

    ret20 = (last / closes[-21] - 1.0) * 100 if len(closes) >= 21 and closes[-21] else 0.0
    rets = []
    for i in range(max(1, len(closes) - 21), len(closes)):
        if closes[i - 1]:
            rets.append(closes[i] / closes[i - 1] - 1.0)
    vol20 = statistics.pstdev(rets) * 100 if len(rets) >= 2 else 0.0
    vol_ratio = 0.0
    if len(vols) >= 21:
        base = sum(vols[-21:-1]) / 20.0
        if base > 0:
            vol_ratio = vols[-1] / base
    return ("收盘 %s｜20日 %+.2f%%｜MA5 %s｜MA20 %s｜20日波动 %.2f%%｜量比 %.2f"
            % (_fmt(last, 3), ret20, _fmt(ma(5), 2), _fmt(ma(20), 2), vol20, vol_ratio))


def _indicator_cached(conn, game, symbol):
    """同一对局同一天只算一次指标，3 个 AI 直接复用。"""
    gid = game["id"]
    date = game["cursor_date"]
    ent = _IND_CACHE.get(gid)
    if not ent or ent[0] != date:
        ent = (date, {})
        _IND_CACHE[gid] = ent
        if len(_IND_CACHE) > _IND_CACHE_MAX:
            for key in list(_IND_CACHE)[:-_IND_CACHE_MAX]:
                _IND_CACHE.pop(key, None)
    if symbol not in ent[1]:
        ent[1][symbol] = _indicator_text(conn, game, symbol)
    return ent[1][symbol]


def _build_ai_prompt(conn, game, player):
    gid = game["id"]
    syms = _symbols(conn, gid, True)
    cursor = game["cursor_date"]
    cash = float(player["cash"])
    mv = 0.0
    pos_lines = []
    for pos in _positions(conn, gid, player["id"]):
        last = _last_close(conn, gid, pos["symbol"], cursor)
        value = float(pos["qty"]) * last
        mv += value
        nm = conn.execute("SELECT name FROM symbols WHERE game_id=? AND symbol=?",
                          (gid, pos["symbol"])).fetchone()
        pos_lines.append("- %s %s %s %s｜成本 %s｜现价 %s｜市值 %s" % (
            pos["symbol"], (nm["name"] if nm else pos["symbol"]), _qnum(pos["qty"]),
            "枚" if is_crypto(pos["symbol"]) else "股",
            _fmt(pos["avg_cost"], 3), _fmt(last, 3), _fmt(value, 2)))
    equity = cash + mv
    journal_rows = conn.execute(
        "SELECT date,text FROM journals WHERE game_id=? AND player_id=? "
        "ORDER BY date DESC LIMIT 5", (gid, player["id"])).fetchall()
    journal = "\n".join("- %s：%s" % (r["date"], r["text"]) for r in journal_rows) or "（还没有）"
    watch = []
    for s in syms:
        watch.append("- %s %s：%s" % (s["symbol"], s["name"],
                                      _indicator_cached(conn, game, s["symbol"])))
    persona = next((p for p in PERSONAS if p["id"] == player["persona"]), None)
    style = persona["style"] if persona else "稳健、分散、控制回撤。"
    crypto = str(game.get("market") or "stock") == "crypto"
    unit = "USDT" if crypto else "元"
    if crypto:
        rules = (
            "1. 只能买卖观察池里的标的；不能做空、不能加杠杆、不能买观察池外的币。\n"
            "2. 数量按币计价，最小 0.000001；卖出不能超过持仓（7×24，无 T+1）。\n"
            "3. 不设单币仓位上限，但不要一次把现金打光。\n"
            "4. 订单在下一交易日开盘成交，按开盘价加滑点；币市没有涨跌停。\n"
            "5. 每天最多 5 笔订单；没有把握就空仓观望。\n\n"
        )
    else:
        rules = (
            "1. 只能买卖观察池里的标的；不能做空、不能加杠杆、不能买观察池外的股票。\n"
            "2. 买入必须是 100 股整数倍；卖出不能超过可卖数量（T+1，今天买的明天才能卖）。\n"
            "3. 单只标的市值不超过总资产的 35%%。\n"
            "4. 订单在下一交易日开盘成交，按开盘价加滑点；涨停买不进、跌停卖不出。\n"
            "5. 每天最多 5 笔订单；没有把握就空仓观望。\n\n"
        )
    example = "BTCUSDT" if crypto else "sh600519"
    example_qty = "0.01" if crypto else "100"
    pos_text = "\n".join(pos_lines) if pos_lines else "（空仓）"
    watch_text = "\n".join(watch)
    return (
        "你正在参加一场" + ("币市" if crypto else "A 股") + "模拟盘，扮演「" + player["name"] + "」。\n"
        "你的风格：" + style + "\n\n"
        "当前模拟日：" + cursor + "（你只能使用这一天及以前的数据，绝不能假设未来）\n"
        "账户：现金 " + _fmt(cash) + " " + unit + "｜持仓市值 " + _fmt(mv) + " " + unit
        + "｜总资产 " + _fmt(equity) + " " + unit + "\n"
        "持仓：\n" + pos_text + "\n\n"
        "观察池（均线、涨跌幅、波动率都按截至今日收盘计算）：\n" + watch_text + "\n\n"
        "你最近的复盘：\n" + journal + "\n\n"
        "交易规则：\n" + rules +
        "请只输出一个 JSON 对象，不要任何解释或 Markdown：\n"
        '{"analysis":{"kind":"个股","symbols":["' + example + '"],'
        '"keywords":["放量","突破"],"text":"三五句分析：看到了什么、依据是什么、风险在哪"},'
        '"orders":[{"symbol":"' + example + '","side":"buy","qty":' + example_qty
        + ',"reason":"一句话理由"}],"journal":"一句话复盘"}\n'
        "analysis.kind 只能是：大盘 / 个股 / 持仓 / 风险 / 机会 / 复盘 之一；"
        "analysis.symbols 只能是观察池里的代码；keywords 给 2~4 个词。\n"
        "不交易就把 orders 写成空数组。"
    )


def _extract_json(text):
    """从模型回复里提取第一个合法 JSON 对象（允许前后有解释文字）。"""
    s = str(text or "")
    start = s.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        pass
                    break
        start = s.find("{", start + 1)
    return None


def _call_ai(prompt):
    api_key, api_url, api_model = get_api_conf("chat")
    if not (api_key or "").strip():
        raise StockError("尚未配置聊天 API 密钥，AI 无法决策（设置 → 接入与密钥）")
    messages = [
        {"role": "system", "content": "你是模拟盘交易员。只输出合法 JSON，不输出解释。"},
        {"role": "user", "content": prompt},
    ]
    text = llm._quick_chat(api_key, api_url, api_model, messages,
                           max_tokens=700, source="stock") or ""
    if not _extract_json(text):
        # 给一次纠正机会：轻量模型偶尔会带前言或 Markdown
        messages2 = messages + [
            {"role": "assistant", "content": text[:1500]},
            {"role": "user",
             "content": "上面的内容不是合法 JSON。请只输出一个 JSON 对象，"
                        "格式为 {\"orders\":[...],\"journal\":\"...\"}，不要任何其他文字。"},
        ]
        text = llm._quick_chat(api_key, api_url, api_model, messages2,
                               max_tokens=700, source="stock") or ""
    return text


def _normalize_side(v):
    x = str(v or "").strip().lower()
    if x in ("sell", "s", "卖出", "卖"):
        return "sell"
    if x in ("buy", "b", "买入", "买"):
        return "buy"
    return ""


def _validate_ai_orders(conn, game, player, parsed):
    """把 AI 的订单逐笔校验后写入 pending；非法订单只记录在返回值里。"""
    gid = game["id"]
    cursor = game["cursor_date"]
    accepted, rejected = [], []
    orders = parsed.get("orders") if isinstance(parsed, dict) else None
    if not isinstance(orders, list):
        orders = []
    p = _player(conn, gid, player["id"])
    cash_left = float(p["cash"]) - _pending_buy_cost(conn, game, p["id"])
    equity = float(p["cash"])
    pos_value = {}
    for pos in _positions(conn, gid, p["id"]):
        v = float(pos["qty"]) * _last_close(conn, gid, pos["symbol"], cursor)
        pos_value[pos["symbol"]] = v
        equity += v
    pending_sell = {}
    for o in _pending_orders(conn, gid, p["id"]):
        if o["side"] == "sell":
            pending_sell[o["symbol"]] = pending_sell.get(o["symbol"], 0) + float(o["qty"])
    for item in orders[:MAX_ORDERS_PER_DAY * 2]:
        if len(accepted) >= MAX_ORDERS_PER_DAY:
            rejected.append({"symbol": "", "side": "", "qty": 0,
                             "reason": "单日最多 %d 笔订单" % MAX_ORDERS_PER_DAY})
            break
        if not isinstance(item, dict):
            rejected.append({"symbol": "", "side": "", "qty": 0, "reason": "订单格式不对"})
            continue
        raw_sym = item.get("symbol") or item.get("code") or ""
        side = _normalize_side(item.get("side"))
        try:
            s = normalize_symbol(raw_sym)
        except StockError as e:
            rejected.append({"symbol": str(raw_sym), "side": side, "qty": 0,
                             "reason": str(e)})
            continue
        try:
            qty = float(item.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if is_crypto(s):
            qty = round(qty, CRYPTO_QTY_DECIMALS)
        reason = str(item.get("reason") or "")[:160]
        if not _is_tradable(conn, gid, s):
            rejected.append({"symbol": s, "side": side, "qty": qty,
                             "reason": "不在本局观察池"})
            continue
        if side not in ("buy", "sell"):
            rejected.append({"symbol": s, "side": side, "qty": qty,
                             "reason": "买卖方向不合法"})
            continue
        if qty <= 0 or (lot_size(s) and qty % lot_size(s) != 0):
            rejected.append({"symbol": s, "side": side, "qty": qty,
                             "reason": "数量必须是 %d 的整数倍" % lot_size(s)
                             if lot_size(s) else "数量必须大于 0"})
            continue
        px = _last_close(conn, gid, s, cursor)
        if px <= 0:
            rejected.append({"symbol": s, "side": side, "qty": qty,
                             "reason": "没有可用行情"})
            continue
        amount = px * qty
        crypto = is_crypto(s)
        if side == "buy":
            if (not crypto and equity > 0
                    and pos_value.get(s, 0) + amount > equity * MAX_SINGLE_POSITION + 1e-6):
                rejected.append({"symbol": s, "side": side, "qty": qty,
                                 "reason": "超过单票 35% 仓位上限"})
                continue
            cost = amount + calc_fee("buy", amount)
            if cash_left + 1e-6 < cost:
                rejected.append({"symbol": s, "side": side, "qty": qty,
                                 "reason": "现金不足"})
                continue
            cash_left -= cost
            pos_value[s] = pos_value.get(s, 0) + amount
        else:
            pos = _position(conn, gid, p["id"], s)
            avail = (float(pos["qty"]) if pos else 0.0) if crypto \
                else (float(pos["available_qty"]) if pos else 0.0)
            if avail - pending_sell.get(s, 0) < qty - 1e-9:
                rejected.append({"symbol": s, "side": side, "qty": qty,
                                 "reason": "可卖数量不足" if crypto else "可卖数量不足（T+1）"})
                continue
            pending_sell[s] = pending_sell.get(s, 0) + qty
        oid = "so_" + uuid.uuid4().hex[:10]
        conn.execute(
            "INSERT INTO orders(id,game_id,player_id,created_date,symbol,side,qty,status,"
            "reason,reject,fill_date,fill_price,fee,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, gid, p["id"], cursor, s, side, qty, "pending", reason, "",
             "", 0, 0, time.time()))
        accepted.append({"id": oid, "symbol": s, "side": side, "qty": qty,
                         "reason": reason})
    return accepted, rejected


def _ai_result(conn, game, player):
    cursor = game["cursor_date"]
    rows = conn.execute(
        "SELECT o.*, s.name FROM orders o LEFT JOIN symbols s "
        "ON s.game_id=o.game_id AND s.symbol=o.symbol "
        "WHERE o.game_id=? AND o.player_id=? AND o.created_date=? AND o.status='pending'",
        (game["id"], player["id"], cursor)).fetchall()
    journal = conn.execute(
        "SELECT text FROM journals WHERE game_id=? AND player_id=? AND date=?",
        (game["id"], player["id"], cursor)).fetchone()
    analysis = _row(conn.execute(
        "SELECT id,player_id,date,kind,text,keywords,created_at FROM analyses "
        "WHERE game_id=? AND player_id=? AND date=? ORDER BY created_at DESC LIMIT 1",
        (game["id"], player["id"], cursor)).fetchone())
    if analysis:
        analysis["symbols"] = _symbols_of_analyses(conn, [analysis["id"]]).get(analysis["id"], [])
    return {
        "accepted": [_row(r) for r in rows],
        "rejected": [],
        "journal": (journal["text"] if journal else ""),
        "analysis": analysis,
        "reused": True,
    }


def ai_decide(game_id, player_id):
    """让一个 AI 参与者对当前模拟日做决策。已决策则幂等返回。"""
    init_db()
    key = "%s:%s" % (game_id, player_id)
    with _GAME_LOCK:
        conn = get_db()
        game = _game(conn, game_id)
        if game["status"] != "running":
            raise StockError("这一局已经结束了")
        player = _player(conn, game_id, player_id)
        if player["kind"] != "ai":
            raise StockError("只有 AI 参与者需要自动决策")
        cursor = game["cursor_date"]
        if _journal_exists(conn, game_id, player_id, cursor):
            return _ai_result(conn, game, player)
        if _orders_exist(conn, game_id, player_id, cursor):
            # 上次可能写完订单但没写复盘；补一条即可
            conn.execute(
                "INSERT OR REPLACE INTO journals(game_id,player_id,date,text,created_at) "
                "VALUES(?,?,?,?,?)",
                (game_id, player_id, cursor, "（本轮已下单）", time.time()))
            conn.commit()
            return _ai_result(conn, game, player)
        if key in _AI_BUSY:
            raise StockError("这个 AI 正在决策，请稍候")
        _AI_BUSY.add(key)
        prompt = _build_ai_prompt(conn, game, player)
    try:
        text = _call_ai(prompt)
        parsed = _extract_json(text)
        if parsed is None:
            raise StockError("AI 没有返回可解析的订单 JSON，请重试")
    finally:
        with _GAME_LOCK:
            _AI_BUSY.discard(key)
    with _GAME_LOCK:
        conn = get_db()
        game2 = _game(conn, game_id)
        if game2["cursor_date"] != cursor:
            raise StockError("模拟日已经推进，本次 AI 决策作废，请重新决策")
        if _journal_exists(conn, game_id, player_id, cursor):
            return _ai_result(conn, game2, _player(conn, game_id, player_id))
        accepted, rejected = _validate_ai_orders(conn, game2, player, parsed)
        journal = str((parsed or {}).get("journal") or "").strip()[:500]
        if not journal:
            journal = "观望" if not accepted else "已提交 %d 笔订单" % len(accepted)
        conn.execute(
            "INSERT OR REPLACE INTO journals(game_id,player_id,date,text,created_at) "
            "VALUES(?,?,?,?,?)", (game_id, player_id, cursor, journal, time.time()))
        # 结构化分析记录（检索用）。模型没按新格式输出时，用订单 + 复盘兜底，
        # 保证历史分析库里永远有可检索的记录。
        parsed_analysis = parsed.get("analysis") if isinstance(parsed, dict) else None
        if not isinstance(parsed_analysis, dict):
            parsed_analysis = {}
        analysis_text = str(parsed_analysis.get("text") or journal).strip()[:2000]
        analysis_kind = parsed_analysis.get("kind") or ("持仓" if accepted else "复盘")
        analysis_symbols = parsed_analysis.get("symbols") or [a["symbol"] for a in accepted]
        analysis = _insert_analysis(
            conn, game_id, player_id, cursor, analysis_kind, analysis_text,
            parsed_analysis.get("keywords") or [], analysis_symbols)
        conn.commit()
        return {"accepted": accepted, "rejected": rejected,
                "journal": journal, "analysis": analysis, "reused": False}
