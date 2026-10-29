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
