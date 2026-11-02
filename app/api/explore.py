# -*- coding: utf-8 -*-
"""后台探索批次 API（自注册）。

`explore(background=true)` 只返回批次号；回合结束后 SSE 已经关闭，
后台线程再 emit 的事件前端收不到。这里把 status / result / cancel 暴露成
普通 HTTP 接口，前端在消息里挂一张卡轮询，就能看到「跑没跑、跑到哪、结论是什么」。
"""
from app import explore
from app.registry import route


def _batch(h):
    return str(h._body().get("batch") or "").strip()


@route("/api/explore/status")
def _api_explore_status(h):
    r = explore.status(_batch(h))
    return h._json(r, 200 if r.get("ok") else 400)


@route("/api/explore/result")
def _api_explore_result(h):
    r = explore.result(_batch(h))
    return h._json(r, 200 if r.get("ok") else 400)


@route("/api/explore/cancel")
def _api_explore_cancel(h):
    r = explore.cancel(_batch(h))
    return h._json(r, 200 if r.get("ok") else 400)
