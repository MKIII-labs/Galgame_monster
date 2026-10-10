# -*- coding: utf-8 -*-
"""账户余额查询：内置 DeepSeek，其它厂商可自定义余额地址"""
from urllib.parse import urlparse

import requests

from app.config import load_config, get_api_conf

DEEPSEEK_BALANCE_PATH = "/user/balance"


def balance_endpoint(provider):
    """返回余额接口地址：优先自定义，其次按 API 地址自动识别 DeepSeek，否则为空"""
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


def query_balance(provider):
    """查询余额。返回 {ok, supported, items, is_available, raw, error}"""
    url = balance_endpoint(provider)
    if not url:
        return {"ok": False, "supported": False,
                "error": "该接口未内置余额查询；如厂商提供，请在下方填写「余额查询地址」"}
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
        for k in ("total_balance", "balance", "available", "remaining", "credit", "quota"):
            if k in data and not isinstance(data[k], (dict, list)):
                items.append({"label": k, "total": data[k], "granted": "", "topped": ""})
    return {"ok": True, "supported": True, "items": items, "is_available": is_available,
            "raw": None if items else data}
