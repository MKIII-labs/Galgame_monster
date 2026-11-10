# -*- coding: utf-8 -*-

import re



def _url_public_error(url):
    """fetch_url 的 SSRF 护栏：只允许公网 http(s)。

    实现放在 app/search.py（抓正文时每跳重定向也要复用同一套判断），这里只委托。
    """
    from app.search import public_url_error
    return public_url_error(url)


def _t_fetch_url(root, args, stop_event=None):
    from .base import (FETCH_URL_CHARS, FETCH_URL_MAX_CHARS)
    url = str((args or {}).get("url") or "").strip()
    err = _url_public_error(url)
    if err:
        return "❌ " + err, None
    if stop_event is not None and stop_event.is_set():
        return "⏹ 已停止", None
    try:
        chars = int((args or {}).get("chars") or FETCH_URL_CHARS)
    except (TypeError, ValueError):
        chars = FETCH_URL_CHARS
    chars = max(800, min(chars, FETCH_URL_MAX_CHARS))
    try:
        from app.search import fetch_text
        body = fetch_text(url, timeout=15,
                          query=str((args or {}).get("query") or ""), chars=chars)
    except Exception as e:
        return "❌ 抓取失败：%s" % e, None
    if not body:
        return ("❌ 抓不到正文（页面可能需要登录、是纯 JS 渲染，或站点拒绝抓取）：%s"
                % url), None
    return "URL: %s\n\n%s" % (url, body), None


_URL_RE = re.compile(r'https?://[^\s<>"\'，。；;)\]】]+', re.I)


_BV_RE = re.compile(r'\b(?:BV[0-9A-Za-z]{10}|av\d+)\b', re.I)


_ABS_TIME_RE = re.compile(r'\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}')


def _extract_abs_time(text):
    m = _ABS_TIME_RE.search(str(text or ""))
    return m.group(0).replace("T", " ") if m else ""


def _extract_url(text):
    m = _URL_RE.search(str(text or ""))
    if m:
        return m.group(0).rstrip(".,;，。；:：")
    m = _BV_RE.search(str(text or ""))
    return m.group(0) if m else ""
