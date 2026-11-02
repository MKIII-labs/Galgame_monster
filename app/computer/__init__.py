# -*- coding: utf-8 -*-
"""Computer Use · 对外入口（工具面）

工具面只暴露 5 个，不按「每个动作一个工具」铺开：
  computer_open / computer_see / computer_act / computer_read / computer_shot

分层：
  policy   → 能不能去（域名 / 敏感站点 / 自身界面）
  browser  → 有没有浏览器、当前在哪一页
  snapshot → 看到什么（结构化优先）
  actions  → 做什么（真实输入事件）
  trace    → 做过什么（可回放）

P0 边界：只操作一个标签页；桌面（UIA）与扩展桥在 P1/P2。
"""
import json
import time

from app.computer import actions, browser, desktop, policy, snapshot, trace

TOOLS = ("computer_open", "computer_see", "computer_act", "computer_read", "computer_shot",
         "computer_windows")

_LAST_SHOT = {}          # 目标 → 上次自动截图时间（防抖，避免每一步都花 1600 token）
AUTO_SHOT_GAP = 30.0


def _auto_shot_enabled():
    try:
        from app.config import load_config
        return bool((load_config() or {}).get("computer_auto_shot", True))
    except Exception:
        return True


def poor_browser_structure(data):
    """结构化信息是否不够用：没有可交互元素，或画布占了小半屏。纯函数，便于测试。"""
    d = data or {}
    if not int(d.get("count") or 0):
        return True
    vw, vh = int(d.get("vw") or 0), int(d.get("vh") or 0)
    area = 0
    for c in (d.get("canvases") or []):
        area += int(c.get("w") or 0) * int(c.get("h") or 0)
    return bool(vw and vh and area / float(vw * vh) > 0.4)


def poor_desktop_structure(data):
    return not int((data or {}).get("count") or 0)


def maybe_auto_shot(key, poor, shooter):
    """结构不足时补一张截图（带冷却）。返回要追加的文本或空串。"""
    if not poor or not _auto_shot_enabled():
        return ""
    now = time.time()
    if now - float(_LAST_SHOT.get(key) or 0) < AUTO_SHOT_GAP:
        return ""
    _LAST_SHOT[key] = now
    try:
        desc = shooter()
    except Exception as e:
        return "\n\n（自动截图失败：%s）" % e
    if not desc:
        return ""
    return "\n\n【结构化信息不足，已自动截图】\n" + desc


def enabled():
    try:
        from app.config import load_config
        return bool((load_config() or {}).get("computer_enabled"))
    except Exception:
        return False


def _cfg():
    try:
        from app.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _guard_after(url_before):
    """动作后如果跳到了未授权站点，立刻停止并说明该怎么申请。"""
    url = browser.current_url()
    if not url:
        return ""
    decision, why = policy.check(
        url, None, skip_allowlist=bool(_cfg().get("computer_browser_unrestricted")))
    if decision == "allow":
        return ""
    if decision == "deny":
        return "\n\n🛑 当前页被安全策略拒绝：%s。不要再尝试操作这个页面。" % why
    return ("\n\n⚠️ 当前页 %s 未授权：%s。要操作它，请调用 computer_open 让用户确认。"
            % (url[:120], why))


# ============================================================
# 五个工具
# ============================================================

def open_url(url, sandbox=None):
    # 「无限制」开关：不再要求站点白名单；内置禁区 / 设置里的禁止站点 / 自身界面仍拒绝
    skip = bool(_cfg().get("computer_browser_unrestricted"))
    decision, why = policy.check(url, sandbox, skip_allowlist=skip)
    if decision == "deny":
        return "❌ " + why
    if decision == "ask":
        return ("❌ 站点未授权：%s。\n"
                "请调用 computer_open 让我先确认（或由用户在「设置 → Computer Use」里加白名单）。"
                % why)
    try:
        browser.ensure()
        final, title = browser.navigate(url)
    except Exception as e:
        return "❌ 打开失败：%s" % e
    trace.add("computer_open", {"url": url}, "ok", "", final)
    data = None
    try:
        data = snapshot.collect(0, 30)
        text = snapshot.render(data, _cfg().get("computer_snapshot_chars") or 4000)
    except Exception as e:
        text = "（快照获取失败：%s）" % e
    tail = maybe_auto_shot("browser", poor_browser_structure(data), shot)
    return "已打开：%s（%s）\n\n%s%s" % (title or "(无标题)", final, text, tail)


def see(**kw):
    start = kw.get("start") or 0
    limit = kw.get("limit") or 30
    try:
        browser.ensure()
        data = snapshot.collect(int(start), int(limit))
    except Exception as e:
        return "❌ 获取快照失败：%s" % e
    text = snapshot.render(data, _cfg().get("computer_snapshot_chars") or 4000)
    return text + maybe_auto_shot("browser", poor_browser_structure(data), shot)


def act(action, ref=None, value="", clear=False):
    try:
        browser.ensure()
    except Exception as e:
        return "❌ 浏览器不可用：%s" % e
    before = browser.current_url()
    text = actions.act(action, ref, value, clear)
    trace.add("computer_act", {"action": action, "ref": ref, "value": value}, text, before,
              browser.current_url())
    return text + _guard_after(before)


def read(kind="console", ref=None):
    kind = str(kind or "console").lower()
    if kind == "trace":
        return trace.recent(12)
    try:
        browser.ensure()
    except Exception as e:
        return "❌ 浏览器不可用：%s" % e
    if kind == "url":
        return browser.current_url() or "（当前没有页面）"
    if kind == "value":
        if not ref:
            return "❌ value 需要 ref（元素编号）"
        try:
            v = browser.evaluate(
                "(function(){var el=window.__gm&&window.__gm.els[%d];"
                "return el?String(el.value===undefined?'':el.value):null;})()" % int(ref))
            return "（空）" if v in (None, "") else "值：%s" % str(v)[:500]
        except Exception as e:
            return "❌ 读取失败：%s" % e
    if kind == "console":
        return _console_text()
    if kind == "network":
        return _network_text()
    return "❌ 不支持的 kind：%s（可选 console / network / url / value / trace）" % kind


def _console_text():
    c = browser.conn()
    lines = []
    for ev in c.events("Runtime.consoleAPICalled", 20):
        kind = ev.get("type") or "log"
        parts = []
        for a in (ev.get("args") or []):
            v = a.get("value")
            parts.append(str(v) if v is not None else (a.get("description") or a.get("type") or ""))
        lines.append("[%s] %s" % (kind, " ".join(parts)[:300]))
    for ev in c.events("Runtime.exceptionThrown", 8):
        d = ev.get("exceptionDetails") or {}
        desc = (d.get("exception") or {}).get("description") or d.get("text") or ""
        where = (" @ %s:%s" % (d.get("url", "").split("/")[-1], d.get("lineNumber")))
        lines.append("[error] %s%s" % (desc[:300], where if d.get("url") else ""))
    for ev in c.events("Log.entryAdded", 20):
        e = ev.get("entry") or {}
        if e.get("level") in ("error", "warning"):
            lines.append("[%s] %s %s" % (e.get("level"), (e.get("text") or "")[:240],
                                         ("@%s:%s" % ((e.get("url") or "").split("/")[-1],
                                                      e.get("lineNumber") or ""))
                                         if e.get("url") else ""))
    if not lines:
        return "控制台没有错误或警告。"
    return "最近的控制台输出（最多 20 条）：\n" + "\n".join(lines[-20:])


def _network_text():
    c = browser.conn()
    reqs = c.events("Network.requestWillBeSent", 40)
    resps = c.events("Network.responseReceived", 40)
    status = {}
    for ev in resps:
        r = ev.get("response") or {}
        status[ev.get("requestId")] = "%s %s" % (r.get("status"), r.get("mimeType") or "")
    lines = []
    for ev in reqs[-25:]:
        req = ev.get("request") or {}
        rid = ev.get("requestId")
        lines.append("%s %s → %s" % (req.get("method") or "GET",
                                     (req.get("url") or "")[:160],
                                     status.get(rid) or "（无响应）"))
    if not lines:
        return "没有捕获到网络请求（Network 域在页面加载后才开启，可刷新页面试试）。"
    return "最近的请求（最多 25 条）：\n" + "\n".join(lines)


def shot():
    try:
        browser.ensure()
        res = browser.conn().send("Page.captureScreenshot", {"format": "png"}, timeout=30)
    except Exception as e:
        return "❌ 截图失败：%s" % e
    import base64
    b64 = (res or {}).get("data") or ""
    if not b64:
        return "❌ 截图失败：浏览器没有返回图像"
    try:
        from app.webprobe import _describe_shot
        desc = _describe_shot(b64)
    except Exception as e:
        desc = "⚠️ 视觉描述不可用：%s（图像已截取，但无法描述）" % e
    trace.add("computer_shot", {}, "ok", browser.current_url(), browser.current_url())
    return "【截图（%d KB）】\n%s" % (len(b64) // 1024, desc)


# ============================================================
# 统一分发
# ============================================================

def dispatch(name, args=None, sandbox=None):
    args = args if isinstance(args, dict) else {}
    target = str(args.get("target") or "").strip()
    win = target.startswith("w")          # w1 / w2 …：桌面窗口编号
    # 桌面能力有独立开关，先按 surface 路由，再判断浏览器开关
    if name == "computer_windows":
        return desktop.list_windows()
    if win:
        if name == "computer_see":
            text = desktop.snapshot(target, args.get("start"), args.get("limit"))
            return text + maybe_auto_shot(
                target, poor_desktop_structure(desktop.last_snapshot(target)),
                lambda: desktop.shot(target))
        if name == "computer_act":
            before = desktop.window_title(target)
            text = desktop.act(target, args.get("action"), args.get("ref"),
                               args.get("value", ""), bool(args.get("clear")))
            trace.add("computer_act",
                      {"target": target, "action": args.get("action"),
                       "ref": args.get("ref"), "value": args.get("value")},
                      text, before, desktop.window_title(target))
            return text
        if name == "computer_read":
            kind = str(args.get("kind") or "console").lower()
            if kind == "value":
                return desktop.read_value(target, args.get("ref"))
            if kind == "trace":
                return trace.recent(12)
            return "❌ 桌面窗口只支持 kind=value / trace（当前：%s）" % kind
        if name == "computer_shot":
            return desktop.shot(target)
        return "❌ 桌面窗口不支持这个工具：%s" % name
    if not enabled():
        return ("⚠️ Computer Use 未开启。请在「设置 → Agent → Computer Use」打开后再试。")
    try:
        if name == "computer_open":
            return open_url(str(args.get("url") or "").strip(), sandbox)
        if name == "computer_see":
            return see(start=args.get("start"), limit=args.get("limit"))
        if name == "computer_act":
            return act(args.get("action"), args.get("ref"),
                       args.get("value", ""), bool(args.get("clear")))
        if name == "computer_read":
            return read(args.get("kind") or "console", args.get("ref"))
        if name == "computer_shot":
            return shot()
    except Exception as e:
        return "❌ Computer Use 出错：%s" % e
    return "❌ 未知的 Computer 工具：%s" % name


def judge(name, args=None, sandbox=None, unrestricted=False):
    """审批前的预判：返回 (decision, reason, host)。

    unrestricted（完全放权）：站点白名单 / 高危动作不再询问，但内置金融禁区、
    设置里的禁止站点、本项目界面仍然拒绝（见 policy.check）。
    """
    args = args if isinstance(args, dict) else {}
    target = str(args.get("target") or "").strip()
    if name == "computer_windows" or target.startswith("w"):
        decision, why = desktop.judge(name, args, unrestricted=unrestricted)
        return decision, why, ""
    if not enabled():
        return "deny", "Computer Use 未开启", ""
    if name == "computer_open":
        url = str(args.get("url") or "").strip()
        skip = unrestricted or bool(_cfg().get("computer_browser_unrestricted"))
        decision, why = policy.check(url, sandbox, skip_allowlist=skip)
        return decision, why, policy.host_of(url)
    if name == "computer_act":
        risk, why = actions.classify(args.get("action"), args.get("ref"))
        if risk == "deny":
            return "deny", why, ""
        if risk == "high" and not unrestricted:
            return "ask", why, ""
        return "allow", "", ""
    return "allow", "", ""
