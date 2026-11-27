# -*- coding: utf-8 -*-
"""Computer Use · 桌面后端（纯函数与白名单，不启动任何窗口）"""
from app.computer import desktop, winapi


# ---------------------------------------------------------------- 白名单
def test_exe_allowed_exact_and_wildcard():
    pats = ["notepad.exe", "code.exe"]
    assert desktop.exe_allowed(r"C:\Windows\notepad.exe", pats)
    assert desktop.exe_allowed(r"D:\Tools\Code.exe", pats)          # 大小写不敏感
    assert not desktop.exe_allowed(r"C:\Windows\mspaint.exe", pats)
    assert desktop.exe_allowed("chrome.exe", ["*.exe"])
    assert not desktop.exe_allowed("", pats)


def test_exe_allowed_uses_config_when_patterns_none(monkeypatch):
    monkeypatch.setattr(desktop, "allowed_exes", lambda: ["notepad.exe"])
    assert desktop.exe_allowed("C:/x/Notepad.exe")
    assert not desktop.exe_allowed("C:/x/winword.exe")


# ---------------------------------------------------------------- 快照排版
def _data(**kw):
    d = {"key": "w1", "title": "记事本", "exe": "notepad.exe", "count": 3, "start": 0,
         "items": [
             {"ref": 1, "type": "Button", "name": "保存", "x": 10, "y": 20, "enabled": True},
             {"ref": 2, "type": "Edit", "name": "密码", "x": 30, "y": 40,
              "enabled": True, "pass": True},
             {"ref": 3, "type": "Edit", "name": "文本编辑器", "x": 50, "y": 60,
              "enabled": False},
         ]}
    d.update(kw)
    return d


def test_render_marks_password_and_disabled():
    text = desktop.render(_data())
    assert "# 桌面窗口 w1 · 记事本 · notepad.exe" in text
    assert "[1] Button 「保存」" in text
    assert "password ← 禁止读取/填写" in text
    assert "禁用" in text


def test_render_pagination_and_empty():
    text = desktop.render(_data(count=30))
    assert "共 30 个控件" in text and "start=3" in text
    empty = desktop.render({"key": "w2", "title": "x", "exe": "a.exe", "items": [], "count": 0})
    assert "没有可操作控件" in empty


def test_render_includes_readonly_texts():
    """结果 / 状态文字要出现在快照里（计算器显示「显示为 15」这类）。"""
    text = desktop.render(_data(texts=["显示为 15"]))
    assert "文字（只读，不能点击）" in text
    assert "「显示为 15」" in text


def test_is_glyph_skips_icon_font():
    assert desktop._is_glyph("\ue81c")
    assert not desktop._is_glyph("显示为 15")


# ---------------------------------------------------------------- 风险分级
def _install_refs():
    desktop._REFS["w1"] = {
        1: (None, {"ref": 1, "name": "保存", "pass": False}),
        2: (None, {"ref": 2, "name": "密码", "pass": True}),
        3: (None, {"ref": 3, "name": "删除账号", "pass": False}),
    }


def test_classify_blocks_password_typing():
    _install_refs()
    try:
        assert desktop.classify("w1", "type", 2)[0] == "deny"
        assert desktop.classify("w1", "click", 3)[0] == "high"
        assert desktop.classify("w1", "click", 1)[0] == "low"
        assert desktop.classify("w1", "dance", 1)[0] == "deny"
    finally:
        desktop._REFS.pop("w1", None)


# ---------------------------------------------------------------- 缺依赖
def test_list_windows_reports_missing_dependency(monkeypatch):
    monkeypatch.setattr(desktop, "enabled", lambda: True)
    monkeypatch.setattr(desktop, "allowed_exes", lambda: ["notepad.exe"])
    monkeypatch.setattr(desktop, "available", lambda: False)
    text = desktop.list_windows()
    assert text.startswith("❌") and "uiautomation" in text


def test_window_reports_missing_dependency(monkeypatch):
    monkeypatch.setattr(desktop, "available", lambda: False)
    monkeypatch.setattr(desktop, "exe_allowed", lambda exe, patterns=None: True)
    desktop._WINDOWS["w_x"] = {"hwnd": 1, "pid": 1, "exe": "notepad.exe", "title": "x"}
    try:
        ctrl, err = desktop._window("w_x")
        assert ctrl is None and "uiautomation" in err
    finally:
        desktop._WINDOWS.pop("w_x", None)


# ---------------------------------------------------------------- 输入光标
def test_type_moves_caret_to_end(monkeypatch):
    """SetValue 之后要把光标送到末尾，否则下一次 Enter / Tab 会插到开头。"""
    calls = []

    class VP:
        Value = "old"

        def SetValue(self, v):
            calls.append(v)

    class Ctrl:
        def GetValuePattern(self):
            return VP()

    monkeypatch.setattr(desktop, "_caret_to_end",
                        lambda ctrl, hwnd: calls.append("end"))
    note = desktop._do_type(Ctrl(), {"name": "x"}, "abc", False, 1)
    assert calls == ["oldabc", "end"]
    assert "SetValue" in note


# ---------------------------------------------------------------- 按键 / 点击
def test_norm_key_is_case_insensitive():
    assert desktop._norm_key("enter") == "Enter"
    assert desktop._norm_key("RETURN") == "Enter"
    assert desktop._norm_key("down") == "ArrowDown"
    assert desktop._norm_key("") == "Enter"
    assert desktop._norm_key("F5") == "F5"


def test_window_retries_transient_com_error(monkeypatch):
    calls = {"n": 0}

    class Auto:
        def ControlFromHandle(self, hwnd):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("事件无法调用任何订户")
            return "CTRL"

    monkeypatch.setattr(desktop, "available", lambda: True)
    monkeypatch.setattr(desktop, "exe_allowed", lambda exe, patterns=None: True)
    monkeypatch.setattr(desktop, "_uia", lambda: Auto())
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    desktop._WINDOWS["w_r"] = {"hwnd": 1, "pid": 1, "exe": "x.exe", "title": "x"}
    try:
        ctrl, err = desktop._window("w_r")
        assert ctrl == "CTRL" and err == ""
        assert calls["n"] == 2
    finally:
        desktop._WINDOWS.pop("w_r", None)


def _click_env(monkeypatch, clicks):
    monkeypatch.setattr(desktop.winapi, "set_foreground", lambda hwnd: True)
    monkeypatch.setattr(desktop.winapi, "force_foreground", lambda hwnd: True)
    monkeypatch.setattr(desktop.winapi, "send_click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)

    class Pat:
        def Invoke(self):
            pass

    class Ctrl:
        Name = "进入微信"
        IsEnabled = True

        def GetInvokePattern(self):
            return Pat()

    class Win:
        NativeWindowHandle = 1

        class R:
            def width(self):
                return 420

            def height(self):
                return 570

        BoundingRectangle = R()

        def GetChildren(self):
            return []

    return Win(), Ctrl()


def test_click_falls_back_when_invoke_does_nothing(monkeypatch):
    """自绘界面的 Invoke 假成功：界面没变化要补一次真实鼠标点击。"""
    clicks = []
    win, ctrl = _click_env(monkeypatch, clicks)
    out = desktop._do_click(win, ctrl, {"ref": 5, "name": "进入微信", "x": 100, "y": 200})
    assert "真实鼠标点击" in out
    assert clicks == [(100, 200)]


def test_click_does_not_retry_high_risk_button(monkeypatch):
    clicks = []
    win, ctrl = _click_env(monkeypatch, clicks)
    out = desktop._do_click(win, ctrl, {"ref": 5, "name": "提交订单", "x": 100, "y": 200})
    assert "Invoke" in out and "真实鼠标点击" not in out
    assert clicks == []


# ---------------------------------------------------------------- 坐标动作 / 窗口信息
def _act_env(monkeypatch):
    clicks = []
    monkeypatch.setattr(desktop, "enabled", lambda: True)
    monkeypatch.setattr(desktop.stop, "is_stopped", lambda: False)
    monkeypatch.setattr(desktop, "exe_allowed", lambda exe, patterns=None: True)
    monkeypatch.setattr(desktop.winapi, "is_elevated", lambda pid: False)
    monkeypatch.setattr(desktop, "_window", lambda key: ("CTRL", ""))
    monkeypatch.setattr(desktop, "snapshot", lambda *a, **k: "BRIEF")
    monkeypatch.setattr(desktop.winapi, "set_foreground", lambda hwnd: True)
    monkeypatch.setattr(desktop.winapi, "force_foreground", lambda hwnd: True)
    monkeypatch.setattr(desktop.winapi, "raise_window", lambda hwnd: True)
    monkeypatch.setattr(desktop.winapi, "window_at", lambda x, y: 1)
    monkeypatch.setattr(desktop.winapi, "root_window", lambda hwnd: 1)
    monkeypatch.setattr(desktop.winapi, "send_click", lambda x, y: clicks.append((x, y)))
    desktop._WINDOWS["w_c"] = {"hwnd": 1, "pid": 1, "exe": "x.exe", "title": "t"}
    return clicks


def test_act_coordinate_click_without_ref(monkeypatch):
    """自绘界面没有控件编号：允许直接给截图坐标点击。"""
    clicks = _act_env(monkeypatch)
    try:
        out = desktop.act("w_c", "click", x=10, y=20)
        assert "已点击截图坐标 (10,20)" in out
        assert clicks == [(10, 20)]
    finally:
        desktop._WINDOWS.pop("w_c", None)


def test_to_screen_uses_shot_origin(monkeypatch):
    """截图坐标 + 原点 = 屏幕坐标（点击与命中的内部换算）。"""
    monkeypatch.setattr(desktop, "_local_origin", lambda info: (100, 50))
    assert desktop._to_screen({}, 10, 20) == (110, 70)


def test_annotate_shot_draws_grid(tmp_path):
    from PIL import Image

    p = tmp_path / "shot.png"
    Image.new("RGB", (320, 220), "white").save(str(p))
    assert desktop._annotate_shot(str(p)) == (320, 220)
    im = Image.open(str(p)).convert("RGB")
    assert im.getpixel((100, 6)) != (255, 255, 255)


def test_act_requires_ref_or_coords(monkeypatch):
    _act_env(monkeypatch)
    try:
        out = desktop.act("w_c", "click")
        assert "需要 ref 或坐标" in out
    finally:
        desktop._WINDOWS.pop("w_c", None)


def test_render_self_drawn_hints_coordinates():
    text = desktop.render({"key": "w1", "title": "微信", "exe": "Weixin.exe",
                           "count": 0, "items": []})
    assert "x / y" in text and "没有可操作控件" in text


def test_read_window_reports_self_drawn(monkeypatch):
    class Node:
        ControlTypeName = "PaneControl"

        def GetChildren(self):
            return []

    class Win:
        ControlTypeName = "WindowControl"

        def GetChildren(self):
            return [Node(), Node()]

    monkeypatch.setattr(desktop, "available", lambda: True)
    monkeypatch.setattr(desktop, "exe_allowed", lambda exe, patterns=None: True)
    monkeypatch.setattr(desktop, "_window", lambda key: (Win(), ""))
    monkeypatch.setattr(desktop.winapi, "class_name", lambda hwnd: "Qt5QWindowIcon")
    desktop._WINDOWS["w_s"] = {"hwnd": 9, "pid": 1, "exe": "Weixin.exe", "title": "微信"}
    try:
        out = desktop.read_window("w_s")
        assert "自绘界面" in out
        assert "Qt5QWindowIcon" in out
        assert "UIA 节点：2" in out
    finally:
        desktop._WINDOWS.pop("w_s", None)


# ---------------------------------------------------------------- 审批
def test_judge_requires_enabled(monkeypatch):
    monkeypatch.setattr(desktop, "enabled", lambda: False)
    assert desktop.judge("computer_windows", {})[0] == "deny"


def test_judge_unknown_window(monkeypatch):
    monkeypatch.setattr(desktop, "enabled", lambda: True)
    assert desktop.judge("computer_act", {"target": "w999", "action": "click",
                                          "ref": 1})[0] == "deny"


def test_judge_requires_whitelist(monkeypatch):
    monkeypatch.setattr(desktop, "enabled", lambda: True)
    monkeypatch.setattr(desktop, "exe_allowed", lambda exe, patterns=None: False)
    desktop._WINDOWS["w9"] = {"hwnd": 1, "pid": 1, "exe": "evil.exe", "title": "x"}
    try:
        assert desktop.judge("computer_act", {"target": "w9", "action": "click",
                                              "ref": 1})[0] == "deny"
    finally:
        desktop._WINDOWS.pop("w9", None)


# ---------------------------------------------------------------- 急停
def test_stop_toggle():
    from app.computer import stop
    stop.clear()
    assert not stop.is_stopped()
    stop.toggle("test")
    try:
        assert stop.is_stopped()
        assert "急停" in stop.state_text()
    finally:
        stop.clear()
    assert not stop.is_stopped()


# ---------------------------------------------------------------- winapi 基本可用
def test_winapi_self_process():
    import os
    exe = winapi.exe_of(os.getpid())
    assert exe.lower().endswith(".exe")
    assert isinstance(winapi.is_elevated(os.getpid()), bool)
