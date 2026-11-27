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
import time

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
MAX_TEXTS = 12           # 只读文字上限（结果 / 状态这类，不能点击）
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


def unrestricted_enabled():
    """无限制：不检查 exe 白名单（提权窗口 / 密码框等硬限制仍生效）。"""
    return bool(_cfg().get("computer_desktop_unrestricted"))


def allowed_exes():
    return [str(x).strip().lower() for x in (_cfg().get("computer_apps") or []) if str(x).strip()]


def exe_allowed(exe, patterns=None):
    """exe 是否在白名单里（支持 *.exe 与完整路径）。纯函数，便于测试。"""
    if not exe:
        return False
    if patterns is None and unrestricted_enabled():
        return True
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


_UIA_HINT = ("桌面操作依赖 uiautomation（Windows UIA），当前 Python 环境里没有安装。"
             "请在应用使用的 Python 环境执行 `pip install uiautomation`"
             "（或 `pip install -r requirements.txt`）后重启应用。")


def available():
    """UIA 依赖是否可用：缺依赖时给可执行的提示，而不是抛 ImportError。"""
    try:
        import uiautomation  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_hotkey():
    """第一次用桌面能力时注册急停热键（全局 Ctrl+Alt+Shift+Esc）。"""
    winapi.register_stop_hotkey(lambda: stop.toggle("hotkey"))


# ============================================================
# 窗口列表
# ============================================================

def list_windows():
    if not enabled():
        return ("⚠️ 桌面操作未开启：设置 → Agent → Computer Use → 「允许操作桌面程序」"
                "（改完点页面底部「保存」才生效）。")
    if not unrestricted_enabled() and not allowed_exes():
        return ("⚠️ 桌面程序白名单是空的。请先在设置里填入允许操作的 exe 名"
                "（如 notepad.exe），或打开「无限制」开关；否则任何窗口都不会列出来。")
    if not available():
        return "❌ " + _UIA_HINT
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
        scope = "无限制模式" if unrestricted_enabled() else ("白名单（%s）" % "、".join(allowed_exes()))
        return ("%s当前没有可见窗口。可以先把目标程序打开，再让我列一次。" % scope)
    lines = ["可用窗口（%s）："
             % ("无限制模式：所有可见程序" if unrestricted_enabled() else "只有白名单内的程序")]
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
    if not available():
        return None, _UIA_HINT
    ctrl, err = None, None
    # UIA 在窗口切换 / 界面重绘时会抛瞬态 COM 错误（「事件无法调用任何订户」等）：
    # 直接判「窗口已关闭」会逼模型放弃，先短重试两次再报错。
    for _ in range(3):
        try:
            ctrl = _uia().ControlFromHandle(info["hwnd"])
            err = None
            break
        except Exception as e:             # noqa: BLE001
            ctrl, err = None, e
            time.sleep(0.15)
    if ctrl is None:
        _WINDOWS.pop(str(key), None)       # 编号已失效：让模型重新枚举
        return None, ("读取窗口失败（可能已关闭或界面正在切换）：%s。"
                      "重新 computer_windows 取新编号后再试。" % err)
    return ctrl, ""


def window_title(key):
    return (_WINDOWS.get(str(key or "")) or {}).get("title") or ""


# ============================================================
# 快照
# ============================================================

# 截图坐标原点：截图是窗口客户区，坐标动作与快照都统一用「截图坐标（左上角 0,0）」。
_SHOT_ORIGIN = {}


def _local_origin(info):
    """截图坐标系原点（屏幕物理像素）：优先用最近一次截图的实测原点。"""
    hwnd = int((info or {}).get("hwnd") or 0)
    hit = _SHOT_ORIGIN.get(hwnd)
    if hit:
        # 窗口移动过就作废：旧截图的坐标已对不上，按当前客户区重新算（必要时重截图）
        if hit[2] and hit[2] == winapi.window_rect(hwnd):
            return hit[0], hit[1]
        _SHOT_ORIGIN.pop(hwnd, None)
    org = winapi.client_origin(hwnd)
    if org:
        return org
    rect = winapi.window_rect(hwnd)
    return (rect[0], rect[1]) if rect else (0, 0)


def _to_screen(info, x, y):
    """截图坐标 → 屏幕坐标（window_at / send_click 用屏幕坐标）。"""
    ox, oy = _local_origin(info)
    return ox + int(x), oy + int(y)

def _is_glyph(name):
    """图标字体（私有区字符）不算可读文字。"""
    return all(0xE000 <= ord(c) <= 0xF8FF or c.isspace() for c in name)


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
    items, mapping, texts = [], {}, []
    ox, oy = _local_origin(info)
    queue = [(ctrl, 0, "")]
    visited = 0
    ref = 0
    while queue and visited < MAX_NODES and len(items) < MAX_ITEMS:
        node, depth, parent_type = queue.pop(0)
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
                if ctype == "TextControl":
                    # 结果 / 状态这类只读文字也带上：否则点完「=」模型看不到结果
                    if (name and name not in texts and len(name) >= 2
                            and parent_type not in INTERESTING
                            and len(texts) < MAX_TEXTS and not _is_glyph(name)):
                        texts.append(name[:60])
                    continue
                if ctype not in INTERESTING or not name:
                    queue.append((ch, depth + 1, ctype))
                    continue
                rect = ch.BoundingRectangle
                ref += 1
                sx, sy = (int(rect.left + rect.width() / 2),
                          int(rect.top + rect.height() / 2))
                meta = {
                    "ref": ref,
                    "type": ctype.replace("Control", ""),
                    "name": name[:48],
                    "enabled": bool(ch.IsEnabled),
                    "pass": _is_password(ch),
                    # x/y 是截图坐标（左上角 0,0），与 computer_shot 的网格一致
                    "x": sx - ox,
                    "y": sy - oy,
                    # sx/sy 是屏幕坐标，内部点击 / 命中校验用
                    "sx": sx,
                    "sy": sy,
                    "w": int(rect.width()),
                    "h": int(rect.height()),
                }
                mapping[ref] = (ch, meta)
                items.append(meta)
                queue.append((ch, depth + 1, ctype))
            except Exception:
                continue
    _REFS[str(key)] = mapping
    data = {"key": str(key), "title": info["title"], "exe": os.path.basename(info["exe"]),
            "count": len(items), "start": start, "items": items[start:start + limit],
            "texts": texts}
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
    if items:
        lines.append("坐标为截图坐标：左上角 (0,0)，x 向右、y 向下（与 computer_shot 网格一致）")
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
    texts = d.get("texts") or []
    if texts:
        lines.append("")
        lines.append("文字（只读，不能点击）：")
        lines.extend("「%s」" % t for t in texts)
    if count > shown:
        lines.append("（共 %d 个控件，已显示 %d–%d；继续用 start=%d）"
                     % (count, start + 1, shown, shown))
    elif count == 0:
        lines.append("（没有可操作控件：可能是自绘界面。用 computer_shot 看画面，"
                     "再用 computer_act 带 x / y 点坐标；输入支持中文粘贴）")
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


def _hit_error(hwnd, x, y):
    """坐标是否落在目标窗口上（先置顶，避免点/输入落到别的窗口）。"""
    winapi.raise_window(hwnd)
    hit = winapi.window_at(x, y)
    if hit and int(hwnd) not in (hit, winapi.root_window(hit)):
        return "❌ 坐标 (%d,%d) 命中的不是目标窗口，已取消（请重新截图确认位置）。" % (x, y)
    return ""


def _click_at(hwnd, x, y):
    err = _hit_error(hwnd, x, y)
    if err:
        return err
    winapi.send_click(x, y)
    return ""


def _wait_foreground(hwnd, timeout=0.6):
    """点击后窗口激活需要一点时间：轮询前台，别一看到旧句柄就放弃。"""
    hwnd = int(hwnd)
    end = time.time() + max(0.05, timeout)
    while time.time() < end:
        fg = winapi.foreground_hwnd()
        if fg == hwnd or winapi.root_window(fg) == hwnd:
            return True
        time.sleep(0.05)
    return False


def _coord_type(info, x, y, text, clear):
    """按截图坐标聚焦后输入文本：中文走剪贴板 + Ctrl+V（SendKeys 对中文不稳）。"""
    sx, sy = _to_screen(info, x, y)
    err = _click_at(info["hwnd"], sx, sy)
    if err:
        return err
    hwnd = int(info["hwnd"])
    if not _wait_foreground(hwnd):
        # 点击一般会激活窗口；仍失败时才尝试强制置前，避免输入落到别的窗口
        winapi.force_foreground(hwnd)
        if not _wait_foreground(hwnd, 0.4):
            return "❌ 点击后目标窗口仍未激活，已取消输入（避免落到别的窗口）。"
    auto = _uia()
    if clear:
        auto.SendKeys("{Ctrl}a", interval=0.01)
        auto.SendKeys("{Delete}", interval=0.01)
    if winapi.set_clipboard_text(text):
        auto.SendKeys("{Ctrl}v", interval=0.01)
        return "已在截图坐标 (%d,%d) 粘贴 %d 个字符" % (int(x), int(y), len(text))
    auto.SendKeys(text, interval=0.01)
    return "已在截图坐标 (%d,%d) 输入 %d 个字符（键盘回退）" % (int(x), int(y), len(text))


def act(key, action, ref=None, value="", clear=False, x=None, y=None):
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
    try:
        pos = (int(x), int(y)) if x is not None and y is not None else None
    except (TypeError, ValueError):
        pos = None
    target, meta = _ctl(key, ref)
    if act_name in ("click", "type", "hover") and not meta and pos is None:
        return ("❌ 需要 ref 或坐标 (x,y)：先 computer_see 取编号；"
                "自绘界面用 computer_shot 看画面后直接给 x / y。")

    try:
        if act_name == "click":
            if pos:
                sx, sy = _to_screen(info, *pos)
                err = _click_at(info["hwnd"], sx, sy)
                if err:
                    return err
                note = "已点击截图坐标 (%d,%d)" % pos
            else:
                note = _do_click(ctrl, target, meta)
        elif act_name == "type":
            if meta and meta.get("pass"):
                return "❌ 密码框：按安全策略不读取、不填写。"
            if pos:
                note = _coord_type(info, pos[0], pos[1], str(value), bool(clear))
            else:
                note = _do_type(target, meta, str(value), bool(clear), info["hwnd"])
        elif act_name == "press":
            if pos:
                sx, sy = _to_screen(info, *pos)
                err = _click_at(info["hwnd"], sx, sy)
                if err:
                    return err
                time.sleep(0.1)
            note = _do_press(target, str(value or "Enter"), info["hwnd"])
        elif act_name == "hover":
            if pos:
                sx, sy = _to_screen(info, *pos)
                err = _hit_error(info["hwnd"], sx, sy)
                if err:
                    return err
                winapi.send_move(sx, sy)
                note = "已移到截图坐标 (%d,%d)" % pos
            else:
                sx, sy = meta.get("sx", meta["x"]), meta.get("sy", meta["y"])
                winapi.send_move(sx, sy)
                note = "已移到「%s」上" % meta.get("name")
        else:
            try:
                dy = int(value or 600)
            except ValueError:
                dy = 600
            if pos:
                sx, sy = _to_screen(info, *pos)
            elif meta:
                sx, sy = meta.get("sx", meta["x"]), meta.get("sy", meta["y"])
            else:
                rect = winapi.window_rect(info["hwnd"])
                sx, sy = (rect[0] + 400, rect[1] + 300) if rect else (600, 400)
            err = _hit_error(info["hwnd"], sx, sy)
            if err:
                return err
            winapi.send_wheel(sx, sy, -dy)
            note = "已滚动 %d" % dy
    except Exception as e:
        return "❌ 操作失败：%s" % e
    brief = snapshot(key, 0, 12)
    return "%s\n\n%s" % (note, brief)


def read_window(key):
    """窗口客观信息（标题 / 进程 / 类名 / UIA 节点数）：替代模型现场写 PowerShell 探测。"""
    info = _WINDOWS.get(str(key or ""))
    if not info:
        return "❌ 窗口编号 %s 不存在：先 computer_windows。" % key
    if not exe_allowed(info["exe"]):
        return "❌ 程序 %s 不在白名单，拒绝读取。" % os.path.basename(info["exe"])
    if not available():
        return "❌ " + _UIA_HINT
    ctrl, err = _window(key)
    kinds, nodes = [], 0
    if ctrl is not None:
        queue = [(ctrl, 0)]
        while queue and nodes < 300:
            node, depth = queue.pop(0)
            if depth > 6:
                continue
            try:
                for ch in node.GetChildren():
                    nodes += 1
                    t = (ch.ControlTypeName or "").replace("Control", "")
                    if t and len(kinds) < 8 and t not in kinds:
                        kinds.append(t)
                    queue.append((ch, depth + 1))
            except Exception:
                continue
    lines = [
        "# 窗口 %s" % key,
        "标题：%s" % (info.get("title") or "（无标题）"),
        "程序：%s · pid=%s" % (os.path.basename(info["exe"]), info.get("pid")),
        "句柄：%s · 类名：%s" % (info.get("hwnd"), winapi.class_name(info.get("hwnd")) or "?"),
        "UIA 节点：%d（%s）" % (nodes, "、".join(kinds) or "无可读类型"),
    ]
    if err:
        lines.append("UIA 读取失败：%s" % err)
    if nodes <= 3:
        lines.append("判断：自绘界面，UIA 读不到内容。用 computer_shot 看画面，"
                     "再用 computer_act 带 x / y 点坐标（输入支持中文粘贴）。")
    return "\n".join(lines)


def _win_sig(window, ctrl=None):
    """点击前后的廉价指纹：窗口大小 / 顶层子节点数 / 前台句柄 / 目标控件名。

    自绘界面（微信等）会暴露 Invoke 但不响应：调用返回成功、界面毫无变化。
    有指纹才能判断这次 Pattern 点击到底有没有生效。
    """
    try:
        r = window.BoundingRectangle
        child = ""
        if ctrl is not None:
            try:
                child = (str(ctrl.Name or ""), bool(ctrl.IsEnabled))
            except Exception:              # noqa: BLE001  控件可能已随窗口销毁
                return None
        return (int(r.width()), int(r.height()), len(window.GetChildren()),
                winapi.foreground_hwnd(), child)
    except Exception:                      # noqa: BLE001
        return None


def _do_click(window, ctrl, meta):
    sig0 = _win_sig(window, ctrl)
    for pat in ("Invoke", "Toggle", "SelectionItem", "ExpandCollapse"):
        try:
            if pat == "Invoke":
                p = ctrl.GetInvokePattern()
                if p:
                    p.Invoke()
                    time.sleep(0.35)
                    unchanged = sig0 is not None and _win_sig(window, ctrl) == sig0
                    name = str(meta.get("name") or "").lower()
                    risky = any(w in name for w in HIGH_RISK_WORDS)
                    if unchanged and not risky:
                        # Invoke 假成功：补一次真实鼠标点击（高危按钮不重试，避免重复提交）
                        if winapi.force_foreground(window.NativeWindowHandle):
                            winapi.send_click(meta.get("sx", meta["x"]),
                                              meta.get("sy", meta["y"]))
                        return ("已点击「%s」（Invoke 无响应，已改用真实鼠标点击）"
                                % meta.get("name"))
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
    winapi.force_foreground(window.NativeWindowHandle)
    winapi.send_click(meta.get("sx", meta["x"]), meta.get("sy", meta["y"]))
    return "已点击「%s」（坐标回退）" % meta.get("name")


def _caret_to_end(ctrl, hwnd):
    """把输入光标移到末尾。

    ValuePattern.SetValue 只改文本、不动光标：设值后光标可能停在开头，
    紧接着的 press（Enter/Tab）就会插到文本最前面（实测记事本出现 "\\rBBBAAA"）。
    这里在 SetValue 之后统一把光标送到末尾，保持“接着写”的语义。
    """
    try:
        winapi.set_foreground(hwnd)
        ctrl.SetFocus()
        _uia().SendKeys("{Ctrl}{End}", interval=0.01)
        return True
    except Exception:
        return False


def _do_type(ctrl, meta, text, clear, hwnd):
    try:
        vp = ctrl.GetValuePattern()
        if vp:
            old = "" if clear else (vp.Value or "")
            vp.SetValue(old + text)
            _caret_to_end(ctrl, hwnd)
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

# 模型经常写小写 / 别名（enter、return、esc、down…）：统一归一，别让大小写毁掉一次点击。
_KEY_ALIASES = {
    "return": "Enter", "enter": "Enter", "tab": "Tab",
    "escape": "Escape", "esc": "Escape", "backspace": "Backspace",
    "delete": "Delete", "del": "Delete", "space": "Space", "spacebar": "Space",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "arrow_up": "ArrowUp", "arrow_down": "ArrowDown",
    "arrow_left": "ArrowLeft", "arrow_right": "ArrowRight",
    "home": "Home", "end": "End",
}


def _norm_key(key):
    k = str(key or "").strip()
    if not k:
        return "Enter"
    if k in KEYMAP:
        return k
    return _KEY_ALIASES.get(k.lower(), k)


def _do_press(ctrl, key, hwnd=None):
    key = _norm_key(key)
    name = KEYMAP.get(key)
    if not name:
        return "❌ 不支持的按键：%s（可用：%s）" % (
            key, "、".join(dict.fromkeys(KEYMAP)))
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

def _annotate_shot(path):
    """在截图上叠加坐标网格（100px/格，每 200px 标数字），返回 (w, h)。

    模型看的是像素，光给窗口截图很难猜准坐标；网格 + 标尺能让它直接读出 x/y。
    没有 Pillow 时静默跳过，不影响截图本身。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size
        draw = ImageDraw.Draw(im, "RGBA")
        for x in range(100, w, 100):
            strong = (x % 200 == 0)
            draw.line([(x, 0), (x, h)], fill=(255, 90, 90, 64 if strong else 22), width=1)
        for y in range(100, h, 100):
            strong = (y % 200 == 0)
            draw.line([(0, y), (w, y)], fill=(90, 160, 255, 64 if strong else 22), width=1)
        try:
            font = ImageFont.load_default(size=14)
        except Exception:
            font = ImageFont.load_default()

        def tag(xy, text):
            try:
                box = draw.textbbox(xy, text, font=font)
                draw.rectangle([box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2],
                               fill=(0, 0, 0, 150))
                draw.text(xy, text, fill=(255, 255, 255, 235), font=font)
            except Exception:
                pass

        for x in range(200, w, 200):
            tag((x + 3, 3), str(x))
        for y in range(200, h, 200):
            tag((3, y + 3), str(y))
        tag((6, max(0, h - 24)), "坐标：左上(0,0)  x→  y↓  网格 100px")
        im.save(path)
        return (w, h)
    except Exception:
        return None


def shot(key, sandbox=None):
    if not enabled():
        return "⚠️ 桌面操作未开启。"
    ctrl, err = _window(key)
    if err:
        return "❌ " + err
    info = _WINDOWS.get(str(key)) or {}
    hwnd = int(info.get("hwnd") or 0)
    if hwnd:
        winapi.force_foreground(hwnd)
        time.sleep(0.12)
    front = winapi.foreground_hwnd()
    path = tempfile.mktemp(suffix=".png", prefix="cu_shot_")
    err = ""
    try:
        ok = ctrl.CaptureToImage(path)
    except Exception as e:
        ok = False
        err = e
    if not ok or not os.path.isfile(path):
        return "❌ 截图失败：这个窗口可能不允许截图（%s）" % err
    # 先用原图做视觉描述（网格会干扰识别），再叠加网格用于展示与坐标动作
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        b64 = ""
    size = _annotate_shot(path)
    if hwnd:
        ox, oy = winapi.client_origin(hwnd) or (0, 0)
        cs = winapi.client_size(hwnd)
        wr = winapi.window_rect(hwnd)
        # 截图尺寸更接近整个窗口（含边框 / 标题栏）时，原点用窗口左上角
        if size and cs and wr:
            if (abs(size[0] - cs[0]) > 12 or abs(size[1] - cs[1]) > 12):
                if (abs(size[0] - (wr[2] - wr[0])) <= 12
                        and abs(size[1] - (wr[3] - wr[1])) <= 12):
                    ox, oy = wr[0], wr[1]
        _SHOT_ORIGIN[hwnd] = (ox, oy, wr)
    try:
        with open(path, "rb") as f:
            shown_b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        shown_b64 = b64
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        from app.computer.shots import save_png
        rel = save_png(shown_b64, sandbox, "win")
    except Exception:
        rel = ""
    try:
        from app.webprobe import _describe_shot
        desc = _describe_shot(b64)
    except Exception as e:
        desc = "⚠️ 视觉描述不可用：%s" % e
    pic = ("\n![实机截图](%s)" % rel) if rel else ""
    warn = ""
    if hwnd and front != hwnd:
        warn = "\n（注意：窗口可能不在前台，截图未必是目标窗口；已尝试置前）"
    grid = " · 网格 100px" if size else ""
    return "【窗口截图 %d KB%s】%s\n%s%s" % (len(shown_b64) // 1024, grid, pic, desc, warn)


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

def judge(name, args, unrestricted=False):
    """返回 (decision, reason)。name 是 computer_act / computer_shot 等。

    unrestricted=True（完全放权档位）：桌面白名单与高危动作都不再询问；
    「无限制」配置开关只跳过 exe 白名单，高危动作仍然问。
    """
    args = args if isinstance(args, dict) else {}
    if not enabled():
        return "deny", "桌面操作未开启"
    un = bool(unrestricted or unrestricted_enabled())
    key = str(args.get("target") or "").strip()
    if name == "computer_windows":
        return "allow", ""
    info = _WINDOWS.get(key)
    if not info:
        return "deny", "窗口 %s 不在本次可用列表里（先 computer_windows）" % key
    if not un and not exe_allowed(info["exe"]):
        return "deny", "程序 %s 不在白名单" % os.path.basename(info["exe"])
    if name == "computer_shot":
        return "allow", ""
    if name == "computer_act":
        risk, why = classify(key, args.get("action"), args.get("ref"))
        if risk == "deny":
            return "deny", why
        if risk == "high" and not unrestricted:
            return "ask", why
    return "allow", ""
