# -*- coding: utf-8 -*-
"""搜索服务商余额：bocha / tavily 解析，volc 明确提示不支持。"""
from app import balance


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _cfg(monkeypatch, provider, key="k"):
    monkeypatch.setattr(balance, "load_config", lambda: {
        "search_provider": provider, "search_api_key": key, "search_balance_url": ""})


def test_bocha_remaining(monkeypatch):
    _cfg(monkeypatch, "bocha")
    monkeypatch.setattr(balance.requests, "get",
                        lambda *a, **kw: _Resp({"success": True, "data": {"remaining": "12.34"}}))
    r = balance.query_balance("search")
    assert r["ok"] is True
    assert "12.34" in r["items"][0]["total"]
    assert r["is_available"] is True


def test_tavily_usage(monkeypatch):
    _cfg(monkeypatch, "tavily")
    monkeypatch.setattr(balance.requests, "get", lambda *a, **kw: _Resp({
        "key": {"usage": 150, "limit": 1000},
        "account": {"current_plan": "Bootstrap", "plan_usage": 500, "plan_limit": 15000},
    }))
    r = balance.query_balance("search")
    assert r["ok"] is True and len(r["items"]) == 2
    assert r["is_available"] is True


def test_volc_reports_no_endpoint(monkeypatch):
    _cfg(monkeypatch, "volc")
    r = balance.query_balance("search")
    assert r["ok"] is False and r["supported"] is False
    assert "控制台" in r["error"]


def test_search_without_key(monkeypatch):
    _cfg(monkeypatch, "bocha", key="")
    r = balance.query_balance("search")
    assert r["ok"] is False and "密钥" in r["error"]


def test_search_custom_url(monkeypatch):
    monkeypatch.setattr(balance, "load_config", lambda: {
        "search_provider": "volc", "search_api_key": "k",
        "search_balance_url": "https://example.com/bal"})
    assert balance.balance_endpoint("search") == "https://example.com/bal"
