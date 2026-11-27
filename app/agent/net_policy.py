# -*- coding: utf-8 -*-
"""网络域名白名单（对齐 Trae 的 sandbox network 策略）。

配置项 `agent_net_allow`：
- 默认 `[]` = 不限制（与原来行为一致）；
- 非空时，只有命中名单的主机才允许被 `fetch_url` / `download_file` 访问；
- 每行一条，支持子域名：写 `github.com` 会放行 `api.github.com`，
  也接受 `*.github.com` / 完整 URL（自动取 host）。

只作用于「Agent 主动联网取内容」的两个工具。`web_search` 属于检索链路，
不走这里的白名单（否则搜索结果站点的域名得一条条加）。
"""
from urllib.parse import urlparse

from app.config import load_config


def _norm_host(value):
    """把配置行 / URL / host 统一成小写主机名（去掉 *.: 端口 / 路径）。"""
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        try:
            s = urlparse(s).hostname or ""
        except ValueError:
            return ""
    else:
        s = s.split("/")[0].split(":")[0]
    if s.startswith("*."):
        s = s[2:]
    return s.strip(".")


def allowlist(conf=None):
    """当前白名单（去重后的主机名列表）；空列表表示不限制。"""
    cfg = conf if isinstance(conf, dict) else (load_config() or {})
    raw = cfg.get("agent_net_allow") or []
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").splitlines()
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        host = _norm_host(item)
        if host and host not in out:
            out.append(host)
    return out


def check_host(host, conf=None):
    """返回 "" 表示放行，否则是拒绝原因（给模型看的文案）。"""
    allow = allowlist(conf)
    if not allow:
        return ""
    h = _norm_host(host)
    if not h:
        return "URL 缺少主机名"
    for domain in allow:
        if h == domain or h.endswith("." + domain):
            return ""
    return ("域名不在白名单：%s（可在「设置 → Agent → 网络域名白名单」添加，"
            "留空 = 不限制）" % h)


def check_url(url, conf=None):
    """对完整 URL 做白名单校验；返回 "" 表示放行。"""
    try:
        host = urlparse(str(url or "").strip()).hostname or ""
    except ValueError:
        return "URL 解析失败"
    return check_host(host, conf)
