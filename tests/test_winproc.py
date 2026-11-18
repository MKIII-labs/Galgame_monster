# -*- coding: utf-8 -*-
"""子进程窗口控制的回归。

背景：本应用常以 pythonw 启动（没有控制台），控制台子进程会各自弹一个黑框，
后台跑命令时非常干扰。这里锁两件事：
1. `no_window()` 在 Windows 必须给非 0 的 creationflags；
2. AI 最常走的两条链路（run_command / 写后 node --check）真的把 flag 传下去了。
"""
import os
import subprocess
import sys

import pytest

from app import agent, winproc


def test_no_window_flag():
    if os.name == "nt":
        assert winproc.no_window() != 0
    else:
        assert winproc.no_window() == 0


def test_run_command_passes_no_window(tmp_path, monkeypatch):
    seen = {}

    class FakeProc:
        pid = 4242
        returncode = 0

        def __init__(self):
            self.stdout = iter([b"ok\n"])     # 读线程按行消费

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kw):
        seen.update(kw)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    text, undo = agent._t_run_command(str(tmp_path), {"argv": ["git", "status"]})
    assert seen.get("creationflags") == winproc.no_window()
    assert "退出码：0" in text
    assert undo is None


def test_write_check_passes_no_window(tmp_path, monkeypatch):
    seen = {}
    js = tmp_path / "a.js"
    js.write_text("var a = 1;\n", encoding="utf-8")

    class FakeRun:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        seen.update(kw)
        return FakeRun()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert agent.post_write_check(str(js)) == ""
    assert seen.get("creationflags") == winproc.no_window()


@pytest.mark.skipif(os.name != "nt", reason="taskkill 只在 Windows 上走这条路")
def test_kill_tree_passes_no_window(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    agent._kill_tree(12345)
    assert seen.get("creationflags") == winproc.no_window()


def _install_once():
    """装一次全局兜底，返回 (包装前的 __init__, 包装前的 _INSTALLED, 最近一次调用的 kwargs)。

    3.13 的 Popen 不再把 creationflags 挂到实例上，所以用一层记录器接住实参。
    """
    original = subprocess.Popen.__init__
    flag = winproc._INSTALLED
    base = getattr(original, "__wrapped__", original)
    seen = {}

    def recorder(self, *args, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return base(self, *args, **kwargs)

    subprocess.Popen.__init__ = recorder
    winproc._INSTALLED = False
    assert winproc.install_default() is True
    return original, flag, seen


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW 只在 Windows 生效")
def test_install_default_injects_flag():
    """没显式传 creationflags 的调用，必须自动补上 CREATE_NO_WINDOW。"""
    original, flag, seen = _install_once()
    try:
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=30)
        assert seen.get("creationflags") == winproc.CREATE_NO_WINDOW
    finally:
        subprocess.Popen.__init__ = original
        winproc._INSTALLED = flag


@pytest.mark.skipif(os.name != "nt", reason="只在 Windows 注入")
def test_install_default_keeps_explicit_flags():
    """显式传了 creationflags 的调用不能被覆盖（弹窗之外的语义要保留）。"""
    original, flag, seen = _install_once()
    try:
        explicit = subprocess.CREATE_NEW_PROCESS_GROUP
        p = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=explicit)
        p.wait(timeout=30)
        assert seen.get("creationflags") == explicit
    finally:
        subprocess.Popen.__init__ = original
        winproc._INSTALLED = flag


@pytest.mark.skipif(os.name != "nt", reason="只在 Windows 注入")
def test_install_default_idempotent():
    """重复安装不应反复包装 Popen。"""
    original, flag, _seen = _install_once()
    try:
        wrapped = subprocess.Popen.__init__
        assert winproc.install_default() is False
        assert subprocess.Popen.__init__ is wrapped
    finally:
        subprocess.Popen.__init__ = original
        winproc._INSTALLED = flag
