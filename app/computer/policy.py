# -*- coding: utf-8 -*-
"""Computer Use · 安全策略层

只做「能不能碰这个地址」的判断，不碰浏览器。
三层闸门：
  1. 硬拒绝：自带敏感站点（银行 / 支付 / 邮箱 / 密码管理器）与自身界面
  2. 白名单：config.computer_sites，为空表示「一律要授权」
  3. 会话内授权：用户点过「本轮都允许」的 host，只活在进程内

设计原则：**宁可多问一次，不可默认放行**。白名单为空时，任何 http(s) 都要授权。
"""
import fnmatch
import os

from app.store import DATA_DIR, ROOT

# 内置敏感站点（P0 不允许通过配置解除）：命中即拒绝，连授权弹窗都不给
SENSITIVE_HOSTS = (
    # 银行
    "*.icbc.com.cn", "*.ccb.com", "*.abchina.com", "*.boc.cn", "*.cmbchina.com",
    "*.bankofchina.com", "*.bankcomm.com", "*.psbc.com", "*.cib.com.cn",
    # 支付
    "*.alipay.com", "*.paypal.com", "*.stripe.com", "*.pay.qq.com",
    # 邮箱
    "mail.google.com", "outlook.live.com", "outlook.office.com",
    "mail.qq.com", "mail.163.com", "mail.126.com",
    # 密码管理器
    "*.1password.com", "*.lastpass.com", "*.bitwarden.com", "*.dashlane.com",
    "*.keeper.io",
)

ALLOWED_SCHEMES = ("http", "https", "file")

_session_granted = set()     # host（小写）→ 用户在本次进程里点过允许


def _cfg():
    try:
        from app.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def own_port():
    """本项目服务的端口：从 data/app_port.txt 读，读不到返回 0。"""
    try:
        with open(os.path.join(DATA_DIR, "app_port.txt"), encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def _host_match(host, pattern):
    host = (host or "").lower().strip(".")
    pattern = (pattern or "").lower().strip()
    if not host or not pattern:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return fnmatch.fnmatchcase(host, pattern)


def _is_loopback(host):
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


def _split(url):
    """最小 URL 解析：不引入 urllib 的宽松规则，只认 scheme://host[:port]/path。"""
    raw = str(url or "").strip()
    if not raw:
        return None
    low = raw.lower()
    for sch in ALLOWED_SCHEMES:
        if low.startswith(sch + "://"):
            rest = raw[len(sch) + 3:]
            break
    else:
        if low.startswith(("file:", "javascript:", "data:", "chrome:", "edge:")):
            return {"scheme": low.split(":", 1)[0], "host": "", "port": 0, "path": raw}
        return None
    # 去掉查询 / 片段再取 host
    head = rest.split("/", 1)[0]
    if "@" in head:                      # user:pass@host 一律不接受
        return None
    path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    port = 0
    host = head
    if head.startswith("["):             # IPv6
        host = head.split("]", 1)[0] + "]"
        tail = head[len(host):]
        if tail.startswith(":"):
            port = int(tail[1:] or 0) if tail[1:].isdigit() else 0
    elif ":" in head:
        host, p = head.rsplit(":", 1)
        port = int(p) if p.isdigit() else 0
    return {"scheme": low.split(":", 1)[0], "host": host.lower(), "port": port, "path": path}


def _inside_web_dir(abs_path):
    try:
        web = os.path.abspath(os.path.join(ROOT, "web")).lower()
        p = os.path.abspath(abs_path).lower()
        return p == web or p.startswith(web + os.sep)
    except Exception:
        return False


def url_of_file(path):
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    out = []
    for ch in p:
        if ch.isalnum() or ch in "/:-._~":
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "file://" + "".join(out)


def check(url, sandbox=None, skip_allowlist=False):
    """判断一个地址能不能去。返回 (decision, reason)。

    decision: allow | ask | deny
    sandbox：Agent 的沙箱根；file:// 只允许沙箱内的 HTML（避免读本机任意文件）。
    skip_allowlist：完全放权档位下不再要求站点白名单，但内置禁区（银行 / 支付 /
    邮箱 / 密码管理器）、设置里的禁止站点、本项目自身界面照旧拒绝。
    """
    info = _split(url)
    if info is None:
        return "deny", "地址格式不支持：%s" % (url or "(空)")
    sch, host = info["scheme"], info["host"]
    cfg = _cfg()

    if sch == "file":
        path = info["path"]
        if path.lower().startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]                       # /D:/x → D:/x
        if _inside_web_dir(path):
            return "deny", "拒绝操作本项目界面（web/ 目录）"
        try:
            from app.agent import resolve_path
            abs_p = resolve_path(sandbox or "", path)
        except Exception:
            return "deny", "file:// 只允许 Agent 沙箱内的 .html（当前没有可用沙箱）"
        if not abs_p.lower().endswith((".html", ".htm")):
            return "deny", "file:// 只允许 .html / .htm"
        return "allow" if skip_allowlist else "ask", \
            "打开沙箱内本地页面 %s" % os.path.basename(abs_p)

    if sch not in ("http", "https"):
        return "deny", "只支持 http / https：%s" % sch

    for pat in SENSITIVE_HOSTS:
        if _host_match(host, pat):
            return "deny", "内置禁区：%s（银行 / 支付 / 邮箱 / 密码管理器不交给 AI 操作）" % host

    # 自身界面：同端口 = 我们自己
    if _is_loopback(host) and own_port() and info["port"] == own_port():
        return "deny", "拒绝操作本项目界面（端口 %d 是本程序）" % own_port()

    for pat in (cfg.get("computer_extra_block") or []):
        if _host_match(host, pat):
            return "deny", "设置里的禁止站点：%s" % pat

    if skip_allowlist:
        return "allow", ""
    for pat in (cfg.get("computer_sites") or []):
        if _host_match(host, pat):
            return "allow", ""
    if host in _session_granted:
        return "allow", ""
    return "ask", "站点 %s 尚未授权（设置 → Computer Use 可加入白名单）" % host


def grant_session(host):
    if host:
        _session_granted.add(str(host).lower())


def clear_session():
    _session_granted.clear()


def host_of(url):
    info = _split(url)
    return (info or {}).get("host") or ""
