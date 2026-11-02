# -*- coding: utf-8 -*-
"""代码库语义索引：状态 / 重建 / 结构化检索（IDE 与 CLI 共用）。"""
from app import codeindex
from app.registry import route


def _body_int(body, key, default):
    try:
        return int(body.get(key) if body.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


@route("/api/codeindex/status")
def _api_codeindex_status(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    st = codeindex.status(sb)
    st.update({"ok": True, "model_ready": codeindex.model_ready(),
               "limits": codeindex._limits()})
    return h._json(st)


@route("/api/codeindex/reindex")
def _api_codeindex_reindex(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    st = codeindex.reindex(sb, background=body.get("background", True) is not False)
    st.update({"ok": True})
    return h._json(st)


@route("/api/codeindex/search")
def _api_codeindex_search(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    q = str(body.get("query") or "").strip()
    if not q:
        return h._json({"ok": False, "error": "缺少 query"}, 400)
    res = codeindex.search_items(sb, q, max(1, min(_body_int(body, "limit", 20), 50)))
    res["ok"] = not res.get("error")
    return h._json(res)
