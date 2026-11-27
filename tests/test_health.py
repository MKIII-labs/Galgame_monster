# -*- coding: utf-8 -*-
"""健康指标：/api/health 已注册，返回存储 / 运行态 / 最近耗时分布。"""
from app import sessions
from app.api import chat, misc


class _H:
    def __init__(self):
        self.obj = None

    def _json(self, payload, code=200):
        self.obj = payload
        return True


def test_health_route_registered():
    from app.registry import snapshot
    import app.api.misc  # noqa: F401  （导入即注册）

    assert ("GET", "/api/health") in snapshot()


def test_health_payload_has_storage_and_timing():
    sessions.save_sessions({"current": "s_h", "sessions": [
        {"id": "s_h", "title": "健康", "updated_at": 1.0, "messages": []}]})
    chat._TIMING_RING.clear()
    chat._TIMING_RING.append({"assemble_ms": 100, "total_ms": 300})
    chat._TIMING_RING.append({"assemble_ms": 300, "total_ms": 500})

    h = _H()
    misc._api_health(h)

    assert h.obj["ok"] is True
    assert h.obj["storage"]["sessions"] == 1
    assert h.obj["storage"]["cache_bytes"] > 0
    assert h.obj["timing"]["assemble_ms"]["n"] == 2
    assert h.obj["timing"]["assemble_ms"]["p50"] in (100, 300)
    assert "active_streams" in h.obj["runtime"]
    chat._TIMING_RING.clear()
