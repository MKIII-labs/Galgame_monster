# -*- coding: utf-8 -*-
"""子进程窗口控制的回归。

背景：本应用常以 pythonw 启动（没有控制台），控制台子进程会各自弹一个黑框，
后台跑命令时非常干扰。这里锁两件事：
1. `no_window()` 在 Windows 必须给非 0 的 creationflags；
2. AI 最常走的两条链路（run_command / 写后 node --check）真的把 flag 传下去了。
"""
import os
import subprocess

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

        def communicate(self, timeout=None):
            return b"ok", None

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
