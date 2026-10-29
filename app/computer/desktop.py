# -*- coding: utf-8 -*-
"""Computer Use · Windows 桌面后端（UIA）

思路：**读控件树，不猜坐标**。
  · 窗口必须先在白名单（设置 → Computer Use → 允许的桌面程序）里，否则连列都不列；
  · 动作优先走 UIA Pattern（Invoke / SetValue / Select / Toggle），不依赖前台焦点；
  · Pattern 不支持才回退到「带窗口到前台 + 真实鼠标键盘」；
  · 密码框只标注、不读值、不填写；提权窗口直接拒绝（UIPI 会静默吞掉输入）。

只支持 Windows；uiautomation 延迟导入，没装或没用到桌面时不初始化 COM。
"""
import base64
import fnmatch
import os
import tempfile

from app.computer import stop, winapi

# 值得暴露给模型的控件类型（Text / Pane 这类容器会淹没信号，不收）
INTERESTING = {
    "ButtonControl", "SplitButtonControl", "EditControl", "ComboBoxControl",
    "CheckBoxControl", "RadioButtonControl", "ListItemControl", "TabItemControl",
    "MenuItemControl", "HyperlinkControl", "SliderControl", "SpinnerControl",
    "TreeItemControl", "DocumentControl",
}
HIGH_RISK_WORDS = ("支付", "付款", "购买", "下单", "转账", "汇款", "删除", "卸载",
                   "格式化", "重置", "注销", "解绑", "提交", "确认支付", "提交订单",
                   "pay", "purchase", "delete", "remove", "unsubscribe", "reset")

MAX_NODES = 600          # 单次遍历的控件上限（大树也不至于卡死）
MAX_ITEMS = 200          # 收集上限
MAX_DEPTH = 12

_WINDOWS = {}            # key -> {hwnd, pid, exe, title}
_REFS = {}               # key -> {ref: (control, meta)}
_LAST = {}               # key -> 最近一次快照的原始数据（自动补截图用）
_SEQ = 0


# ============================================================
# 开关与白名单
# ============================================================

def _cfg():
    try:
        from app.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def enabled():
    return bool(_cfg().get("computer_desktop_enabled"))


def allowed_exes():
    return [str(x).strip().lower() for x in (_cfg().get("computer_apps") or []) if str(x).strip()]


def exe_allowed(exe, patterns=None):
    """exe 是否在白名单里（支持 *.exe 与完整路径）。纯函数，便于测试。"""
    if not exe:
        return False
    patterns = allowed_exes() if patterns is None else patterns
    base = os.path.basename(exe).lower()
    for p in patterns:
        p = p.lower()
        if p == base or p == exe.lower() or fnmatch.fnmatchcase(base, p):
            return True
    return False


def _uia():
    import uiautomation as auto
    return auto


def _ensure_hotkey():
    """第一次用桌面能力时注册急停热键（全局 Ctrl+Alt+Shift+Esc）。"""
    winapi.register_stop_hotkey(lambda: stop.toggle("hotkey"))


# ============================================================
# 窗口列表
# ============================================================

def list_windows():
    if not enabled():
        return "⚠️ 桌面操作未开启：设置 → Agent → Computer Use → 「允许操作桌面程序」。"
    if not allowed_exes():
        return ("⚠️ 桌面程序白名单是空的。请先在设置里填入允许操作的 exe 名"
                "（如 notepad.exe），否则任何窗口都不会列出来。")
    _ensure_hotkey()
    auto = _uia()
    try:
        root = auto.GetRootControl()
    except Exception as e:
        return "❌ 初始化 UIA 失败：%s" % e
    global _SEQ
    rows = []
    for w in root.GetChildren():
        try:
            title = (w.Name or "").strip()
            pid = int(w.ProcessId or 0)
            if not title or not pid or w.IsOffscreen:
                continue
            exe = winapi.exe_of(pid)
            if not exe_allowed(exe):
                continue
            rect = w.BoundingRectangle
            width, height = rect.width(), rect.height()
            if width < 40 or height < 40:
                continue
            hwnd = int(w.NativeWindowHandle or 0)
            if not hwnd:
                continue
            key = ""
            for k, v in _WINDOWS.items():
                if v["hwnd"] == hwnd:
                    key = k
                    break
            if not key:
                _SEQ += 1
                key = "w%d" % _SEQ
                _WINDOWS[key] = {"hwnd": hwnd, "pid": pid, "exe": exe, "title": title}
            else:
                _WINDOWS[key].update({"pid": pid, "exe": exe, "title": title})
            rows.append((key, title, os.path.basename(exe), width, height, pid))
        except Exception:
            continue
    if not rows:
        return ("白名单（%s）当前没有可见窗口。可以先把目标程序打开，再让我列一次。"
                % "、".join(allowed_exes()))
    lines = ["可用窗口（只有白名单内的程序会出现在这里）："]
    for key, title, exe, width, height, pid in rows:
        lines.append("[%s] %s · %s · %s×%s · pid=%d" % (key, title[:60], exe, width, height, pid))
    lines.append("")
    lines.append("接下来用 computer_see target=\"w1\" 看某个窗口的控件。")
    lines.append("急停：Ctrl+Alt+Shift+Esc（%s）" % stop.state_text())
    return "\n".join(lines)


def _window(key):
    info = _WINDOWS.get(str(key or ""))
    if not info:
        return None, "窗口编号 %s 不存在：先调用 computer_windows 看看有哪些窗口。" % key
    if not exe_allowed(info["exe"]):
        return None, "窗口 %s 的程序 %s 已不在白名单里，拒绝操作。" % (key, info["exe"])
    try:
        auto = _uia()
        ctrl = auto.ControlFromHandle(info["hwnd"])
    except Exception as e:
        return None, "窗口可能已关闭：%s" % e
    if ctrl is None:
        return None, "窗口 %s 已经关闭了，请重新 computer_windows。" % key
    return ctrl, ""


def window_title(key):
    return (_WINDOWS.get(str(key or "")) or {}).get("title") or ""


# ============================================================
# 快照
# ============================================================

def snapshot(key, start=0, limit=30):
    ctrl, err = _window(key)
    if err:
        return "❌ " + err
    info = _WINDOWS[str(key)]
    if winapi.is_elevated(info["pid"]):
        return ("❌ 窗口 %s 以管理员权限运行，UIPI 会静默丢弃注入的输入，"
                "按安全策略不操作提权窗口。" % key)
    start = max(0, int(start or 0))
    limit = max(1, min(60, int(limit or 30)))
    items, mapping = [], {}
    queue = [(ctrl, 0)]
    visited = 0
    ref = 0
    while queue and visited < MAX_NODES and len(items) < MAX_ITEMS:
        node, depth = queue.pop(0)
        visited += 1
        if depth >= MAX_DEPTH:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            continue
        for ch in children:
            try:
                if ch.IsOffscreen:
                    continue
                ctype = ch.ControlTypeName or ""
                name = (ch.Name or "").strip()
                if ctype not in INTERESTING or not name:
                    queue.append((ch, depth + 1))
                    continue
                rect = ch.BoundingRectangle
                ref += 1
                meta = {
                    "ref": ref,
                    "type": ctype.replace("Control", ""),
                    "name": name[:48],
                    "enabled": bool(ch.IsEnabled),
                    "pass": _is_password(ch),
                    "x": int(rect.left + rect.width() / 2),
                    "y": int(rect.top + rect.height() / 2),
                    "w": int(rect.width()),
                    "h": int(rect.height()),
                }
                mapping[ref] = (ch, meta)
                items.append(meta)
                queue.append((ch, depth + 1))
            except Exception:
                continue
    _REFS[str(key)] = mapping
    data = {"key": str(key), "title": info["title"], "exe": os.path.basename(info["exe"]),
            "count": len(items), "start": start, "items": items[start:start + limit]}
    _LAST[str(key)] = data
    return render(data)


def last_snapshot(key):
    return _LAST.get(str(key)) or {}


def _is_password(ctrl):
    try:
        return bool(ctrl.IsPassword)
    except Exception:
        return False


def render(data, max_chars=4000):
    """桌面快照排版。纯函数，便于测试。"""
    d = data or {}
    lines = ["# 桌面窗口 %s · %s · %s" % (d.get("key"), d.get("title") or "(无标题)",
                                          d.get("exe") or "?")]
    items = d.get("items") or []
    for it in items:
        bits = ["[%s]" % it.get("ref"), it.get("type") or "?"]
        bits.append("「%s」" % it.get("name"))
        if it.get("pass"):
            bits.append("password ← 禁止读取/填写")
        if not it.get("enabled", True):
            bits.append("禁用")
        bits.append("(%s,%s)" % (it.get("x"), it.get("y")))
        lines.append(" ".join(bits))
    start, count = int(d.get("start") or 0), int(d.get("count") or 0)
    shown = start + len(items)
    if count > shown:
        lines.append("（共 %d 个控件，已显示 %d–%d；继续用 start=%d）"
                     % (count, start + 1, shown, shown))
    elif count == 0:
        lines.append("（没有可操作控件：可能是自绘界面，需要用 computer_shot 看画面）")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n…（快照过长已截断）"
    return out


# ============================================================
# 动作
# ============================================================

def _ctl(key, ref):
    mapping = _REFS.get(str(key)) or {}
    try:
        item = mapping.get(int(ref))
    except (TypeError, ValueError):
        item = None
    if not item:
        return None, None
    return item[0], item[1]


def classify(key, action, ref):
    """动作风险：deny / high / low。给审批层用。"""
    act = str(action or "").lower()
    if act not in ("click", "type", "press", "scroll", "hover"):
        return "deny", "未知动作：%s" % act
    ctrl, meta = _ctl(key, ref)
    if meta is None and act in ("click", "type"):
        return "low", ""
    if act == "type" and meta.get("pass"):
        return "deny", "密码框禁止填写"
    name = (meta.get("name") or "").lower()
    if act in ("click", "press") and any(w in name for w in HIGH_RISK_WORDS):
        return "high", "目标「%s」看起来会删除 / 提交 / 支付" % (meta.get("name") or "")[:24]
    return "low", ""


def act(key, action, ref=None, value="", clear=False):
    if not enabled():
        return "⚠️ 桌面操作未开启。"
    if stop.is_stopped():
        return "⏹ 已急停：%s。告诉我「恢复」后再继续。" % stop.state_text()
    ctrl, err = _window(key)
    if err:
        return "❌ " + err
    info = _WINDOWS[str(key)]
    if winapi.is_elevated(info["pid"]):
        return "❌ 目标是提权窗口，按安全策略不操作。"
    act_name = str(action or "").lower()
    if act_name not in ("click", "type", "press", "scroll", "hover"):
        return "❌ 不支持的动作：%s" % action
    target, meta = _ctl(key, ref)
    if act_name in ("click", "type", "hover") and not meta:
        return "❌ 编号 %s 无效或已过期：请重新 computer_see。" % ref

    try:
        if act_name == "click":
            note = _do_click(ctrl, target, meta)
        elif act_name == "type":
            if meta.get("pass"):
                return "❌ 密码框：按安全策略不读取、不填写。"
            note = _do_type(target, meta, str(value), bool(clear), info["hwnd"])
        elif act_name == "press":
            note = _do_press(target, str(value or "Enter"), info["hwnd"])
        elif act_name == "hover":
            winapi.set_foreground(info["hwnd"])
            winapi.send_move(meta["x"], meta["y"])
            note = "已移到「%s」上" % meta.get("name")
        else:
            try:
                dy = int(value or 600)
            except ValueError:
                dy = 600
            winapi.set_foreground(info["hwnd"])
            winapi.send_wheel(meta["x"] if meta else 600, meta["y"] if meta else 400, -dy)
            note = "已滚动 %d" % dy
    except Exception as e:
        return "❌ 操作失败：%s" % e
    brief = snapshot(key, 0, 12)
    return "%s\n\n%s" % (note, brief)


def _do_click(window, ctrl, meta):
    for pat in ("Invoke", "Toggle", "SelectionItem", "ExpandCollapse"):
        try:
            if pat == "Invoke":
                p = ctrl.GetInvokePattern()
                if p:
                    p.Invoke()
                    return "已点击「%s」（Invoke）" % meta.get("name")
            elif pat == "Toggle":
                p = ctrl.GetTogglePattern()
                if p:
                    p.Toggle()
                    return "已切换「%s」" % meta.get("name")
            elif pat == "SelectionItem":
                p = ctrl.GetSelectionItemPattern()
                if p:
                    p.Select()
                    return "已选中「%s」" % meta.get("name")
            else:
                p = ctrl.GetExpandCollapsePattern()
                if p:
                    state = getattr(p, "ExpandCollapseState", None)
                    if state == 0:            # Collapsed
                        p.Expand()
                        return "已展开「%s」" % meta.get("name")
                    p.Collapse()
                    return "已折叠「%s」" % meta.get("name")
        except Exception:
            continue
    winapi.set_foreground(window.NativeWindowHandle)
    winapi.send_click(meta["x"], meta["y"])
    return "已点击「%s」（坐标回退）" % meta.get("name")


def _do_type(ctrl, meta, text, clear, hwnd):
    try:
        vp = ctrl.GetValuePattern()
        if vp:
            old = "" if clear else (vp.Value or "")
            vp.SetValue(old + text)
            return "已输入 %d 个字符（SetValue）" % len(text)
    except Exception:
        pass
    # 键盘回退：必须先抢到前台，否则按键会落到别的窗口
    winapi.set_foreground(hwnd)
    try:
        ctrl.SetFocus()
    except Exception:
        pass
    auto = _uia()
    if clear:
        auto.SendKeys("{Ctrl}a{Delete}", interval=0.01)
    auto.SendKeys(text, interval=0.01)
    return "已输入 %d 个字符（键盘回退）" % len(text)


KEYMAP = {
    "Enter": "{Enter}", "Tab": "{Tab}", "Escape": "{Esc}", "Esc": "{Esc}",
    "Backspace": "{Backspace}", "Delete": "{Delete}", "Space": " ",
    "ArrowUp": "{Up}", "ArrowDown": "{Down}", "ArrowLeft": "{Left}", "ArrowRight": "{Right}",
    "Home": "{Home}", "End": "{End}",
}


def _do_press(ctrl, key, hwnd=None):
    name = KEYMAP.get(key)
    if not name:
        return "❌ 不支持的按键：%s" % key
    if hwnd:
        winapi.set_foreground(hwnd)
    try:
        ctrl.SetFocus()
    except Exception:
        pass
    _uia().SendKeys(name, interval=0.01)
    return "已按键 %s" % key


# ============================================================
# 截图
# ============================================================

def shot(key):
    if not enabled():
        return "⚠️ 桌面操作未开启。"
    ctrl, err = _window(key)
    if err:
        return "❌ " + err
    path = tempfile.mktemp(suffix=".png", prefix="cu_shot_")
    err = ""
    try:
        ok = ctrl.CaptureToImage(path)
    except Exception as e:
        ok = False
        err = e
    if not ok or not os.path.isfile(path):
        return "❌ 截图失败：这个窗口可能不允许截图（%s）" % err
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        from app.webprobe import _describe_shot
        desc = _describe_shot(b64)
    except Exception as e:
        desc = "⚠️ 视觉描述不可用：%s" % e
    return "【窗口截图 %d KB】\n%s" % (len(b64) // 1024, desc)


def read_value(key, ref):
    """读某个输入框的值（密码框拒绝）。"""
    ctrl, meta = _ctl(key, ref)
    if not meta:
        return "❌ 编号 %s 无效或已过期：请重新 computer_see。" % ref
    if meta.get("pass"):
        return "❌ 密码框：按安全策略不读取。"
    try:
        vp = ctrl.GetValuePattern()
        val = vp.Value if vp else ""
    except Exception as e:
        return "❌ 读取失败：%s" % e
    return "（空）" if not val else "值：%s" % str(val)[:500]


# ============================================================
# 审批
# ============================================================

def judge(name, args):
    """返回 (decision, reason)。name 是 computer_act / computer_shot 等。"""
    args = args if isinstance(args, dict) else {}
    if not enabled():
        return "deny", "桌面操作未开启"
    key = str(args.get("target") or "").strip()
    if name == "computer_windows":
        return "allow", ""
    info = _WINDOWS.get(key)
    if not info:
        return "deny", "窗口 %s 不在本次可用列表里（先 computer_windows）" % key
    if not exe_allowed(info["exe"]):
        return "deny", "程序 %s 不在白名单" % os.path.basename(info["exe"])
    if name == "computer_shot":
        return "allow", ""
    if name == "computer_act":
        risk, why = classify(key, args.get("action"), args.get("ref"))
        if risk == "deny":
            return "deny", why
        if risk == "high":
            return "ask", why
    return "allow", ""
