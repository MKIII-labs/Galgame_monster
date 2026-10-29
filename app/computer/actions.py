# -*- coding: utf-8 -*-
"""Computer Use · 页面动作

动作走 CDP 的 Input 域（真实鼠标/键盘事件），不是 JS 的 el.click()：
  · 游戏 / canvas / 自绘组件只认真实事件；
  · 框架的合成事件链（pointerdown → mousedown → click）也能被正常触发。

只有 <select> 例外：原生下拉是系统弹层，CDP 看不到也点不到，改成设置 value + change。
"""
import json
import time

from app.computer import browser, snapshot
from app.computer.cdp import CDPError

# 这些词出现在按钮/链接文字里 → 视为高风险动作，交给审批层每次询问
HIGH_RISK_WORDS = ("支付", "付款", "购买", "下单", "转账", "汇款", "删除", "卸载",
                   "格式化", "重置", "注销", "解绑", "提交", "确认支付", "提交订单",
                   "pay", "purchase", "delete", "remove", "unsubscribe", "reset")

KEYS = {
    "Enter": (13, "Enter", "\r"),
    "Tab": (9, "Tab", ""),
    "Escape": (27, "Escape", ""),
    "Backspace": (8, "Backspace", ""),
    "Delete": (46, "Delete", ""),
    "Space": (32, "Space", " "),
    "ArrowUp": (38, "ArrowUp", ""),
    "ArrowDown": (40, "ArrowDown", ""),
    "ArrowLeft": (37, "ArrowLeft", ""),
    "ArrowRight": (39, "ArrowRight", ""),
    "Home": (36, "Home", ""),
    "End": (35, "End", ""),
}


def _conn():
    return browser.conn()


def _mouse(cdp, kind, x, y, **kw):
    p = {"type": kind, "x": int(x), "y": int(y)}
    p.update(kw)
    cdp.send("Input.dispatchMouseEvent", p, timeout=10)


def _key(cdp, name):
    code = KEYS.get(name)
    if not code:
        raise CDPError("不支持的按键：%s（可用：%s）" % (name, "、".join(KEYS)))
    vk, c, text = code
    cdp.send("Input.dispatchKeyEvent", {
        "type": "rawKeyDown", "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk, "code": c, "key": name,
    }, timeout=10)
    if text:
        cdp.send("Input.dispatchKeyEvent", {"type": "char", "text": text}, timeout=10)
    cdp.send("Input.dispatchKeyEvent", {
        "type": "keyUp", "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk, "code": c, "key": name,
    }, timeout=10)


def _focus_js(ref, clear=False):
    return r"""
    (function (ref, clear) {
      var el = window.__gm && window.__gm.els[ref];
      if (!el) return false;
      el.scrollIntoView({ block: 'center' });
      el.focus();
      if (clear && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {
        var proto = el.tagName === 'TEXTAREA'
          ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, '');
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return document.activeElement === el;
    })(%d, %s)
    """ % (int(ref), "true" if clear else "false")


def classify(action, ref):
    """动作风险：给审批层用。返回 (risk, 说明)，risk ∈ low / high / deny。"""
    act = str(action or "").lower()
    if act not in ("click", "type", "press", "scroll", "hover"):
        return "deny", "未知动作：%s" % act
    if act == "scroll" or act == "hover":
        return "low", ""
    try:
        info = snapshot.element(ref) if ref else None
    except Exception:
        return "low", ""
    if not info:
        return "low", ""
    if info.get("pass") and act == "type":
        return "deny", "密码框禁止填写"
    text = (info.get("name") or "").lower()
    if act in ("click", "press") and any(w in text for w in HIGH_RISK_WORDS):
        return "high", "目标「%s」看起来会提交订单 / 删除 / 支付" % (info.get("name") or "")[:24]
    return "low", ""


def _brief(url_before):
    """动作后的紧凑反馈：URL 变化 + 前几个元素（省掉一次 see 往返）。"""
    url_after = browser.current_url()
    moved = bool(url_before) and url_after != url_before
    if moved:
        try:
            browser.wait_loaded(timeout=8)
        except Exception:
            pass
    head = "页面已跳转 → %s" % url_after if moved else "页面未跳转（%s）" % (url_after or "?")
    try:
        data = snapshot.collect(0, 10)
        tail = snapshot.render(data, max_chars=1400)
    except Exception as e:
        tail = "（快照获取失败：%s）" % e
    return head + "\n\n" + tail


def act(action, ref=None, value="", clear=False):
    """执行一个动作，返回给模型看的文本。"""
    act_name = str(action or "").lower()
    if act_name not in ("click", "type", "press", "scroll", "hover"):
        return "❌ 不支持的动作：%s（可选 click / type / press / scroll / hover）" % action
    cdp = _conn()
    before = browser.current_url()
    try:
        if act_name in ("click", "hover"):
            if not ref:
                return "❌ %s 需要 ref（先调用 computer_see 拿元素编号）" % act_name
            info = snapshot.element(ref)
            if not info:
                return ("❌ 编号 %s 已失效（页面可能已跳转）。请重新 computer_see 取新编号。"
                        % ref)
            if info.get("disabled"):
                return "❌ [%s]「%s」是禁用状态，点不了。" % (ref, info.get("name"))
            if act_name == "hover":
                _mouse(cdp, "mouseMoved", info["x"], info["y"])
                return "已悬停 [%s]「%s」。\n\n%s" % (ref, info.get("name"), _brief(before))
            if info.get("tag") == "select":
                return ("❌ [%s] 是下拉框，click 无效。请用 type 并给出 value（可选项见 computer_see 的 options）。"
                        % ref)
            _mouse(cdp, "mouseMoved", info["x"], info["y"])
            _mouse(cdp, "mousePressed", info["x"], info["y"], button="left", clickCount=1)
            _mouse(cdp, "mouseReleased", info["x"], info["y"], button="left", clickCount=1)
            return "已点击 [%s]「%s」。\n\n%s" % (ref, info.get("name"), _brief(before))

        if act_name == "type":
            if not ref:
                return "❌ type 需要 ref"
            info = snapshot.element(ref)
            if not info:
                return "❌ 编号 %s 已失效，请重新 computer_see。" % ref
            if info.get("pass"):
                return "❌ [%s] 是密码框：按安全策略不读取、不填写。" % ref
            if info.get("disabled"):
                return "❌ [%s] 是禁用状态，无法输入。" % ref
            if info.get("tag") == "select":
                opts = info.get("options") or []
                want = str(value)
                hit = [o for o in opts if str(o.get("v")) == want or str(o.get("t")) == want]
                if not hit:
                    return ("❌ 下拉框没有选项「%s」。可用：%s"
                            % (want, " / ".join("%s（%s）" % (o.get("t"), o.get("v")) for o in opts)))
                cdp.send("Runtime.evaluate", {"expression": r"""
                  (function (ref, v) {
                    var el = window.__gm.els[ref];
                    el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return el.value;
                  })(%d, %s)
                """ % (int(ref), json.dumps(want)), "returnByValue": True})
                return "已选择 [%s] → %s\n\n%s" % (ref, want, _brief(before))
            ok = browser.evaluate(_focus_js(ref, clear))
            if not ok:
                return "❌ 无法聚焦 [%s]（可能被遮挡）。" % ref
            cdp.send("Input.insertText", {"text": str(value)}, timeout=10)
            return "已在 [%s]「%s」输入 %d 个字符。\n\n%s" % (
                ref, info.get("name"), len(str(value)), _brief(before))

        if act_name == "press":
            if ref:
                browser.evaluate(_focus_js(ref))
            _key(cdp, str(value or "Enter"))
            return "已按键 %s。\n\n%s" % (value, _brief(before))

        if act_name == "scroll":
            try:
                dy = int(value or 600)
            except ValueError:
                dy = 600
            vw = browser.evaluate("innerWidth") or 1280
            vh = browser.evaluate("innerHeight") or 800
            _mouse(cdp, "mouseWheel", vw / 2, vh / 2, deltaX=0, deltaY=dy)
            time.sleep(0.2)
            return "已向下滚动 %d 像素。\n\n%s" % (dy, _brief(before))
    except CDPError as e:
        return "❌ 操作失败：%s" % e
    return "❌ 未执行任何动作"
