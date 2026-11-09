# -*- coding: utf-8 -*-
"""账户余额查询：内置 DeepSeek，其它厂商可自定义余额地址"""
from urllib.parse import urlparse

import requests

from app.config import load_config, get_api_conf

DEEPSEEK_BALANCE_PATH = "/user/balance"

# 搜索服务商的内置余额接口（能用搜索 Key 直接查的）
SEARCH_BALANCE = {
    "bocha": "https://api.bochaai.com/v1/fund/remaining",
    "tavily": "https://api.tavily.com/usage",
}


def _search_conf():
    """搜索服务商的 (provider, key, 余额地址)；volc 没有可用接口，地址为空。"""
    cfg = load_config()
    provider = (cfg.get("search_provider") or "auto").strip() or "auto"
    key = (cfg.get("search_api_key") or "").strip()
    url = (cfg.get("search_balance_url") or "").strip() or SEARCH_BALANCE.get(provider, "")
    return provider, key, url


def balance_endpoint(provider):
    """返回余额接口地址：优先自定义，其次按 API 地址自动识别 DeepSeek，否则为空"""
    if provider == "search":
        _p, _k, url = _search_conf()
        return url
    cfg = load_config()
    custom = (cfg.get(f"{provider}_balance_url") or "").strip()
    if custom:
        return custom
    _, api_url, _ = get_api_conf(provider)
    if "deepseek" in (api_url or "").lower():
        parsed = urlparse(api_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{DEEPSEEK_BALANCE_PATH}"
    return ""


def _search_items(provider, data):
    """把不同搜索服务商的余额响应收敛成统一的 items。"""
    items, is_available = [], None
    if not isinstance(data, dict):
        return items, is_available
    if provider == "bocha":
        # {success, data:{remaining:"10820856.78"}}，单位：元
        rem = (data.get("data") or {}).get("remaining") if isinstance(data.get("data"), dict) else None
        if rem is not None:
            items.append({"label": "余额", "total": "%s 元" % rem, "granted": "", "topped": ""})
            try:
                is_available = float(rem) > 0
            except (TypeError, ValueError):
                pass
        return items, is_available
    if provider == "tavily":
        # {key:{usage,limit}, account:{current_plan,plan_usage,plan_limit,...}}
        k = data.get("key") if isinstance(data.get("key"), dict) else {}
        a = data.get("account") if isinstance(data.get("account"), dict) else {}
        if k:
            items.append({"label": "本 Key 已用", "total": "%s / %s 次" % (
                k.get("usage", "?"), k.get("limit", "?")), "granted": "", "topped": ""})
            try:
                is_available = int(k.get("usage") or 0) < int(k.get("limit") or 0)
            except (TypeError, ValueError):
                is_available = None
        if a:
            plan = a.get("current_plan") or ""
            items.append({"label": "账户", "total": "%s / %s 次%s" % (
                a.get("plan_usage", "?"), a.get("plan_limit", "?"),
                ("（%s）" % plan) if plan else ""), "granted": "", "topped": ""})
        return items, is_available
    return items, is_available


def query_balance(provider):
    """查询余额。返回 {ok, supported, items, is_available, raw, error}"""
    url = balance_endpoint(provider)
    if not url:
        if provider == "search":
            sp, _k, _u = _search_conf()
            if sp == "auto":
                return {"ok": False, "supported": False,
                        "error": "自动模式走各引擎的免注册额度，没有统一余额；"
                                 "填了 Key 的引擎请到各自控制台查看用量"}
            if sp == "volc":
                return {"ok": False, "supported": False,
                        "error": "豆包搜索没有搜索 Key 级别的余额接口；"
                                 "请在火山引擎控制台 → 费用中心查看（每月免费 500 次）"}
            return {"ok": False, "supported": False,
                    "error": "该搜索服务商未内置余额查询；如厂商提供，请填写「余额查询地址」"}
        return {"ok": False, "supported": False,
                "error": "该接口未内置余额查询；如厂商提供，请在下方填写「余额查询地址」"}
    if provider == "search":
        _sp, api_key, _url = _search_conf()
    else:
        api_key, _, _ = get_api_conf(provider)
    if not api_key:
        return {"ok": False, "supported": True, "error": "尚未配置 API 密钥"}
    try:
        resp = requests.get(url, headers={
            "Authorization": f"Bearer {api_key}", "Accept": "application/json"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"ok": False, "supported": True, "error": f"查询失败：{e}"}

    items, is_available = [], None
    if provider == "search":
        _sp, _k, _u = _search_conf()
        items, is_available = _search_items(_sp, data)
    if isinstance(data, dict) and isinstance(data.get("balance_infos"), list):
        # DeepSeek：{is_available, balance_infos:[{currency,total_balance,granted_balance,topped_up_balance}]}
        is_available = data.get("is_available")
        for b in data["balance_infos"]:
            if isinstance(b, dict):
                items.append({"label": b.get("currency", ""), "total": b.get("total_balance", ""),
                              "granted": b.get("granted_balance", ""),
                              "topped": b.get("topped_up_balance", "")})
    if not items and isinstance(data, dict):
        # 通用兜底：尝试搬出常见数值字段
        for k in ("total_balance", "balance", "available", "remaining", "credit", "quota", "credits"):
            if k in data and not isinstance(data[k], (dict, list)):
                items.append({"label": k, "total": data[k], "granted": "", "topped": ""})
    return {"ok": True, "supported": True, "items": items, "is_available": is_available,
            "raw": None if items else data}
