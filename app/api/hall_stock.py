# -*- coding: utf-8 -*-
"""功能大厅：模拟股市 API（自注册，不需要改 httpd 的路由表）。

路由统一为 /api/hall/stock/<动作>，全部 POST，前端继续复用 core.js 的 api()。
"""
from app import stock
from app.registry import route


def _body(h):
    try:
        return h._body() or {}
    except Exception:
        return {}


def _fail(h, e, code=400):
    return h._json({"ok": False, "error": str(e)}, code)


def _want_delta(b):
    """delta=1 时只回增量 patch（前端毫秒级同步）；不传或 0 时保持旧结构，灰度兼容。"""
    v = b.get("delta")
    if v in (None, "", 0, "0", False):
        return False
    return str(v).lower() not in ("0", "false", "no")


@route("/api/hall/stock/search")
def _api_stock_search(h):
    q = str(_body(h).get("q") or "").strip()
    if not q:
        return h._json({"ok": True, "items": []})
    try:
        return h._json({"ok": True, "items": stock.search_symbols(q)})
    except stock.StockError as e:
        return _fail(h, e, 502)


@route("/api/hall/stock/list")
def _api_stock_list(h):
    try:
        return h._json({"ok": True, "games": stock.list_games()})
    except stock.StockError as e:
        return _fail(h, e, 500)


@route("/api/hall/stock/create")
def _api_stock_create(h):
    b = _body(h)
    try:
        game = stock.create_game(
            mode=b.get("mode") or "replay",
            market=b.get("market") or "stock",
            symbols=b.get("symbols") or [],
            days=b.get("days") or 20,
            ai_count=b.get("ai_count", 3),
            initial_cash=b.get("initial_cash") or 1000000,
            name=b.get("name") or "",
            end_date=b.get("end_date") or "",
        )
        return h._json({"ok": True, "game": game})
    except stock.StockError as e:
        return _fail(h, e, 502)


@route("/api/hall/stock/state")
def _api_stock_state(h):
    game_id = str(_body(h).get("game_id") or "")
    try:
        return h._json({"ok": True, "game": stock.game_state(game_id)})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/bars")
def _api_stock_bars(h):
    b = _body(h)
    try:
        bars = stock.bars_for(b.get("game_id"), b.get("symbol"),
                              b.get("limit") or 120,
                              date=b.get("date") or "",
                              with_marks=bool(b.get("marks")))
        if isinstance(bars, dict):
            return h._json({"ok": True, **bars})
        return h._json({"ok": True, "bars": bars})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/trades")
def _api_stock_trades(h):
    b = _body(h)
    try:
        rows = stock.trades_for(b.get("game_id"), player_id=str(b.get("player_id") or ""),
                                symbol=b.get("symbol") or "",
                                date_from=b.get("date_from") or "",
                                date_to=b.get("date_to") or "",
                                limit=b.get("limit") or 200)
        return h._json({"ok": True, "trades": rows})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/order")
def _api_stock_order(h):
    b = _body(h)
    try:
        order = stock.place_order(
            game_id=b.get("game_id"),
            player_id="you",
            symbol=b.get("symbol"),
            side=b.get("side") or "buy",
            qty=b.get("qty") or 0,
            reason=b.get("reason") or "",
        )
        ret = {"ok": True, "order": order}
        if _want_delta(b):
            ret["patch"] = stock.change_patch(b.get("game_id"), "orders")
        else:
            ret["game"] = stock.game_state(b.get("game_id"))
        return h._json(ret)
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/cancel")
def _api_stock_cancel(h):
    b = _body(h)
    try:
        stock.cancel_order(b.get("game_id"), b.get("order_id"), player_id="you")
        ret = {"ok": True}
        if _want_delta(b):
            ret["patch"] = stock.change_patch(b.get("game_id"), "orders")
        else:
            ret["game"] = stock.game_state(b.get("game_id"))
        return h._json(ret)
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/next")
def _api_stock_next(h):
    b = _body(h)
    game_id = str(b.get("game_id") or "")
    try:
        out = stock.next_day(game_id)
        ret = {"ok": True}
        if _want_delta(b) and out.get("game"):
            ret["patch"] = stock.change_patch(game_id, "all")
            ret["game"] = {"id": game_id, "cursor_date": out["game"].get("cursor_date"),
                           "status": out["game"].get("status")}
        else:
            ret["game"] = out.get("game")
        if out.get("no_new_day"):
            ret["no_new_day"] = True
            ret["message"] = out.get("message") or ""
        if out.get("finished"):
            ret["finished"] = True
        return h._json(ret)
    except stock.StockError as e:
        return _fail(h, e, 502)


@route("/api/hall/stock/ai")
def _api_stock_ai(h):
    b = _body(h)
    game_id = str(b.get("game_id") or "")
    player_id = str(b.get("player_id") or "")
    try:
        out = stock.ai_decide(game_id, player_id)
        if _want_delta(b):
            out["patch"] = stock.change_patch(game_id, "ai", player_id)
        else:
            out["game"] = stock.game_state(game_id)
        out["ok"] = True
        return h._json(out)
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/analyses")
def _api_stock_analyses(h):
    b = _body(h)
    try:
        out = stock.search_analyses(
            game_id=str(b.get("game_id") or ""),
            q=b.get("q") or "",
            kind=b.get("kind") or "",
            symbol=b.get("symbol") or "",
            player_id=b.get("player_id") or "",
            date_from=b.get("date_from") or "",
            date_to=b.get("date_to") or "",
            page=b.get("page") or 1,
            size=b.get("size") or 20,
        )
        return h._json({"ok": True, **out})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/analysis_kinds")
def _api_stock_analysis_kinds(h):
    return h._json({"ok": True, "kinds": stock.analysis_kinds(),
                    "engine": stock.fts_mode() or "like"})


@route("/api/hall/stock/timeline")
def _api_stock_timeline(h):
    game_id = str(_body(h).get("game_id") or "")
    try:
        return h._json({"ok": True, "dates": stock.timeline_dates(game_id)})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/state_at")
def _api_stock_state_at(h):
    b = _body(h)
    try:
        return h._json({"ok": True,
                        "state": stock.state_at(b.get("game_id"), b.get("date") or "",
                                               symbol=b.get("symbol") or "",
                                               limit=b.get("limit") or 120)})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/compare")
def _api_stock_compare(h):
    b = _body(h)
    try:
        out = stock.compare_states(b.get("game_id"), b.get("dates") or [],
                                   symbol=b.get("symbol") or "",
                                   limit=b.get("limit") or 120)
        return h._json({"ok": True, **out})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/add_symbol")
def _api_stock_add_symbol(h):
    """中途给已有对局加入一只标的（回放会补齐历史，实时只补到当前模拟日）。"""
    b = _body(h)
    try:
        out = stock.add_symbol(b.get("game_id"), b.get("symbol"))
        return h._json({"ok": True, **out})
    except stock.StockError as e:
        return _fail(h, e, 502)


@route("/api/hall/stock/analysis")
def _api_stock_analysis(h):
    """训练辅助数据一次取齐：市场状态 / 预测 / 命中统计 / 计分板 / 参考散户。"""
    game_id = str(_body(h).get("game_id") or "")
    try:
        return h._json({"ok": True, **stock.analysis_bundle(game_id)})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/predict")
def _api_stock_predict(h):
    b = _body(h)
    game_id = str(b.get("game_id") or "")
    try:
        row = stock.create_prediction(
            game_id=game_id,
            player_id="you",
            target=b.get("target") or "",
            horizon=b.get("horizon") or 3,
            direction=b.get("direction") or "up",
        )
        # 顺手把最新分析数据带回，前端一次请求即可刷新整块训练面板
        return h._json({"ok": True, "prediction": row,
                        "analysis": stock.analysis_bundle(game_id)})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/audit")
def _api_stock_audit(h):
    game_id = str(_body(h).get("game_id") or "")
    try:
        return h._json({"ok": True, "problems": stock.audit_game(game_id)})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/archive")
def _api_stock_archive(h):
    b = _body(h)
    try:
        return h._json({"ok": True,
                        **stock.archive_analyses(b.get("game_id"),
                                                 keep_days=b.get("keep_days") or 0)})
    except stock.StockError as e:
        return _fail(h, e)


@route("/api/hall/stock/delete")
def _api_stock_delete(h):
    game_id = str(_body(h).get("game_id") or "")
    try:
        stock.delete_game(game_id)
        return h._json({"ok": True})
    except stock.StockError as e:
        return _fail(h, e)
