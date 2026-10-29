# -*- coding: utf-8 -*-
"""模拟股市的只读能力：让对话助手「看得见」当前对局。

只读：不下单、不推进交易日、不修改任何对局数据。
所有读取都复用 app.stock 的现有函数，行情天然受 cursor_date 限制，不会提前看到未来。

输出刻意压缩（默认 <1.5KB）：对话上下文很贵，明细交给 detail=1。
"""
from app import stock
from app.capabilities import capability

MAX_POSITIONS = 6
MAX_BARS = 40
MAX_TRADES = 10
MAX_WATCH = 6

NO_GAME = "还没有模拟盘对局。请先在「功能大厅 → 模拟股市」开局。"


def _num(v, n=2):
    try:
        return ("%." + str(n) + "f") % float(v)
    except (TypeError, ValueError):
        return "0"


def _pct(v):
    try:
        return "%+.2f%%" % float(v)
    except (TypeError, ValueError):
        return "0.00%"


def _err(msg):
    return "❌ " + str(msg)


def _pick_game(raw):
    """给定 id 就用它；否则选最近一局进行中的，再退到最近一局。"""
    gid = str(raw or "").strip()
    if gid:
        return gid
    games = stock.list_games(50)
    running = [g for g in games if g.get("status") == "running"]
    if running:
        return running[0]["id"]
    return games[0]["id"] if games else ""


def _names(state):
    return {s.get("symbol"): (s.get("name") or s.get("symbol"))
            for s in (state.get("symbols") or [])}


def _state_text(st, detail=False):
    names = _names(st)
    human = next((p for p in (st.get("players") or []) if p.get("kind") == "human"), {}) or {}
    bm = st.get("benchmark") or {}
    rg = st.get("regime") or {}
    mode = "实时每日盘" if st.get("mode") == "live" else "历史回放"
    remain = ("剩余 %s 个交易日" % st.get("remaining_days")
              if st.get("mode") != "live" else "实时推进")
    lines = [
        "对局：%s｜模拟日 %s｜%s｜下一日 %s｜id=%s" % (
            mode, st.get("cursor_date"), remain, st.get("next_date") or "—",
            st.get("id")),
        "行情状态：%s（沪深300 %s）" % (rg.get("label") or "—", _pct(bm.get("return_pct"))),
    ]
    watch = st.get("symbols") or []
    if watch:
        lines.append("观察池：" + "；".join(
            "%s %s（%s）" % (s.get("name"), _num(s.get("last"), 2), _pct(s.get("change_pct")))
            for s in watch[:MAX_WATCH]))
    lines.append("你：总资产 %s｜现金 %s｜总收益率 %s" % (
        _num(human.get("equity")), _num(human.get("cash")), _pct(human.get("return_pct"))))
    pos = human.get("positions") or []
    if pos:
        lines.append("持仓：")
        limit = 20 if detail else MAX_POSITIONS
        for x in pos[:limit]:
            lines.append("- %s %d 股｜成本 %s｜现价 %s｜%s" % (
                names.get(x.get("symbol"), x.get("symbol")), int(x.get("qty") or 0),
                _num(x.get("avg_cost"), 3), _num(x.get("last"), 3), _pct(x.get("pnl_pct"))))
    else:
        lines.append("持仓：空仓")
    pending = st.get("pending_orders") or []
    if pending:
        lines.append("待成交：" + "；".join(
            "%s %s %d 股" % ("买入" if o.get("side") == "buy" else "卖出",
                            o.get("name") or names.get(o.get("symbol"), o.get("symbol")),
                            int(o.get("qty") or 0))
            for o in pending[:6]))
    ais = [p for p in (st.get("players") or []) if p.get("kind") == "ai"]
    if ais:
        lines.append("AI 决策：" + "；".join(
            "%s %s" % (p.get("name"), "已决策" if p.get("decided") else "未决策") for p in ais))
    return "\n".join(lines)


def _analysis_text(bundle, detail=False):
    den = bundle.get("prediction_stats") or {}
    sc = (bundle.get("scoreboard") or {}).get("stats") or {}
    ref = bundle.get("reference") or {}
    lines = ["市场状态：%s" % ((bundle.get("regime") or {}).get("label") or "—")]
    if den.get("settled") or den.get("pending"):
        text = "预测：已结算 %s｜命中 %s（%s%%）｜待揭晓 %s" % (
            den.get("settled", 0), den.get("hits", 0), den.get("accuracy", 0),
            den.get("pending", 0))
        streak = den.get("streak") or {}
        if streak.get("n"):
            text += "｜当前连续 %s %s" % (streak["n"], "对" if streak.get("type") == "hit" else "错")
        lines.append(text)
        for g in (den.get("by_regime") or [])[:3]:
            lines.append("  %s %s/%s（%s%%）" % (
                g.get("label"), g.get("hits"), g.get("settled"), g.get("accuracy")))
    if sc.get("buy_count") or sc.get("sell_count"):
        lines.append("决策计分板：买入5日胜率 %s/%s（%s%%）｜平均超额 %s｜"
                     "卖出后5日避跌 %s/%s（%s%%）" % (
                         sc.get("buy_win5", 0), sc.get("buy_count", 0),
                         sc.get("buy_win5_pct", 0), _pct(sc.get("buy_avg_excess5")),
                         sc.get("sell_avoid5", 0), sc.get("sell_count", 0),
                         sc.get("sell_avoid5_pct", 0)))
    players = ref.get("players") or []
    if players:
        lines.append("参考散户：")
        for p in players:
            lines.append("- %s[%s] 累计 %s｜近5日 %s｜仓位 %s%%｜换手 %s%%｜集中 %s%%｜回撤 %s%%" % (
                p.get("name"), p.get("style"), _pct(p.get("total_return")),
                _pct(p.get("recent5")), p.get("position_pct", 0), p.get("turnover", 0),
                p.get("concentration", 0), p.get("max_drawdown", 0)))
    return "\n".join(lines)


def _trades_text(game_id, limit=MAX_TRADES):
    sc = stock.trade_scoreboard(game_id, limit=limit)
    rows = sc.get("trades") or []
    if not rows:
        return "还没有成交。"
    lines = ["我的成交（最近 %d 笔；T+5 = 成交后第 5 个交易日涨跌）：" % len(rows)]
    for t in rows[-limit:]:
        fwd = t.get("fwd5")
        extra = _pct(fwd) if fwd is not None else "数据不足"
        if t.get("excess5") is not None:
            extra += "（超额 %s）" % _pct(t.get("excess5"))
        lines.append("- %s %s %s %d 股 @%s｜T+5 %s" % (
            t.get("date"), "买入" if t.get("side") == "buy" else "卖出",
            t.get("name"), int(t.get("qty") or 0), _num(t.get("price"), 3), extra))
    st = sc.get("stats") or {}
    if st.get("buy_count") or st.get("sell_count"):
        lines.append("买入统计：5日胜率 %s/%s（%s%%）｜平均 5 日 %s｜平均超额 %s" % (
            st.get("buy_win5", 0), st.get("buy_count", 0), st.get("buy_win5_pct", 0),
            _pct(st.get("buy_avg_fwd5")), _pct(st.get("buy_avg_excess5"))))
        if st.get("sell_count"):
            lines.append("卖出统计：5日避跌 %s/%s（%s%%）" % (
                st.get("sell_avoid5", 0), st.get("sell_count", 0), st.get("sell_avoid5_pct", 0)))
    return "\n".join(lines)


# ------------------------------------------------------------
# 能力注册
# ------------------------------------------------------------

@capability("stock.list", "列出模拟股市的对局（模式 / 模拟日 / 状态 / 收益 / id）",
            {}, permission="read")
def _cap_list(args, ctx):
    try:
        games = stock.list_games(30)
    except stock.StockError as e:
        return _err(e)
    if not games:
        return "还没有模拟盘对局。请先在「功能大厅 → 模拟股市」开局。"
    lines = ["模拟股市对局（最近的在前）："]
    for g in games[:10]:
        lines.append("- %s｜%s｜模拟日 %s｜%s｜收益 %s｜id=%s" % (
            g.get("name") or "未命名",
            "实时" if g.get("mode") == "live" else "回放",
            g.get("cursor_date"),
            "已结束" if g.get("status") == "finished" else "进行中",
            _pct(g.get("return_pct")), g.get("id")))
    return "\n".join(lines)


@capability("stock.state",
            "当前对局状态：模拟日、行情状态、观察池、我的资产与持仓、待成交、AI 决策情况",
            {"game_id": "str?", "detail": "int?"}, permission="read")
def _cap_state(args, ctx):
    try:
        gid = _pick_game(args.get("game_id"))
        if not gid:
            return NO_GAME
        st = stock.game_state(gid)
        return _state_text(st, detail=bool(args.get("detail")))
    except stock.StockError as e:
        return _err(e)


@capability("stock.bars",
            "某只标的的近 N 日日 K 与买卖标注（含是谁下的单）",
            {"game_id": "str?", "symbol": "str?", "days": "int?"}, permission="read")
def _cap_bars(args, ctx):
    try:
        gid = _pick_game(args.get("game_id"))
        if not gid:
            return NO_GAME
        st = stock.game_state(gid)
        names = _names(st)
        sym = str(args.get("symbol") or "").strip()
        if not sym:
            watch = st.get("symbols") or []
            if not watch:
                return "本局没有可看的标的。"
            sym = watch[0]["symbol"]
        try:
            days = int(args.get("days") or 15)
        except (TypeError, ValueError):
            days = 15
        days = max(5, min(days, MAX_BARS))
        data = stock.bars_for(gid, sym, days, with_marks=True)
        bars = data.get("bars") or []
        if not bars:
            return "没有取到行情。"
        lines = ["%s（%s）近 %d 日，截至 %s：" % (
            names.get(sym, sym), sym, len(bars), data.get("as_of"))]
        for b in bars:
            lines.append("%s 开%s 高%s 低%s 收%s" % (
                b.get("date"), _num(b.get("open"), 3), _num(b.get("high"), 3),
                _num(b.get("low"), 3), _num(b.get("close"), 3)))
        marks = data.get("marks") or []
        if marks:
            lines.append("成交标注：")
            for m in marks[-20:]:
                lines.append("- %s %s %d 股 @%s（%s）" % (
                    m.get("date"), "买入" if m.get("side") == "buy" else "卖出",
                    int(m.get("qty") or 0), _num(m.get("price"), 3),
                    "你" if m.get("player_kind") == "human" else (m.get("player_name") or m.get("player_id"))))
        return "\n".join(lines)
    except stock.StockError as e:
        return _err(e)


@capability("stock.analysis",
            "市场状态、盲预测命中率（按期限/标的/状态）、决策计分板、参考散户对照",
            {"game_id": "str?"}, permission="read")
def _cap_analysis(args, ctx):
    try:
        gid = _pick_game(args.get("game_id"))
        if not gid:
            return NO_GAME
        return _analysis_text(stock.analysis_bundle(gid))
    except stock.StockError as e:
        return _err(e)


@capability("stock.trades",
            "我的成交记录与 T+5 前向收益、超额，以及买入/卖出判断统计",
            {"game_id": "str?", "limit": "int?"}, permission="read")
def _cap_trades(args, ctx):
    try:
        gid = _pick_game(args.get("game_id"))
        if not gid:
            return NO_GAME
        try:
            limit = int(args.get("limit") or MAX_TRADES)
        except (TypeError, ValueError):
            limit = MAX_TRADES
        return _trades_text(gid, max(1, min(limit, 30)))
    except stock.StockError as e:
        return _err(e)


@capability("stock.brief",
            "模拟股市一页纸摘要：当前对局 + 资产持仓 + 行情状态 + 预测命中 + 计分板 + 参考散户",
            {"game_id": "str?"}, permission="read")
def _cap_brief(args, ctx):
    """对话里优先调这个：一次取齐，少来回。"""
    try:
        gid = _pick_game(args.get("game_id"))
        if not gid:
            return NO_GAME
        st = stock.game_state(gid)
        bundle = stock.analysis_bundle(gid)
        parts = [_state_text(st), _analysis_text(bundle), _trades_text(gid, 5)]
        text = "\n\n".join(p for p in parts if p)
        if len(text) > 2200:
            text = text[:2200] + "\n…（已截断）"
        return text
    except stock.StockError as e:
        return _err(e)
