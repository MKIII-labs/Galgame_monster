# -*- coding: utf-8 -*-
"""Computer Use 的「无限制」开关：浏览器放开站点白名单，桌面放开 exe 白名单。"""
import app.config as config_mod
from app import computer
from app.computer import desktop


def _cfg(monkeypatch, **kw):
    cfg = dict(kw)
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    return cfg


def test_browser_unrestricted_allows_any_site(monkeypatch):
    _cfg(monkeypatch, computer_enabled=True, computer_sites=[],
         computer_browser_unrestricted=True)
    decision, why, host = computer.judge("computer_open", {"url": "https://example.com/"})
    assert decision == "allow"
    assert host == "example.com"


def test_browser_default_still_asks(monkeypatch):
    _cfg(monkeypatch, computer_enabled=True, computer_sites=[],
         computer_browser_unrestricted=False)
    decision, _why, _host = computer.judge("computer_open", {"url": "https://example.com/"})
    assert decision == "ask"


def test_browser_unrestricted_keeps_hard_blocks(monkeypatch):
    _cfg(monkeypatch, computer_enabled=True, computer_sites=[],
         computer_browser_unrestricted=True)
    # 邮箱属于内置禁区，开了无限制也不能碰
    decision, why, _host = computer.judge("computer_open", {"url": "https://mail.qq.com/"})
    assert decision == "deny"
    assert "内置禁区" in why


def test_desktop_unrestricted_skips_exe_whitelist(monkeypatch):
    _cfg(monkeypatch, computer_desktop_enabled=True, computer_apps=[],
         computer_desktop_unrestricted=True)
    assert desktop.exe_allowed("whatever.exe") is True
    # 显式传 patterns 时仍按传入的列表判断（纯函数语义不变）
    assert desktop.exe_allowed("whatever.exe", patterns=[]) is False


def test_desktop_default_keeps_whitelist(monkeypatch):
    _cfg(monkeypatch, computer_desktop_enabled=True, computer_apps=["notepad.exe"],
         computer_desktop_unrestricted=False)
    assert desktop.exe_allowed("notepad.exe") is True
    assert desktop.exe_allowed("other.exe") is False
