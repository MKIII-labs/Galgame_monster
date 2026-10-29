# -*- coding: utf-8 -*-
"""Computer Use · 桌面真机联调（会打开一个记事本，测完关掉）

默认跳过：
    set GM_LIVE_DESKTOP=1 && python -m pytest tests/test_computer_desktop_live.py -q
"""
import os
import re
import subprocess
import time

import pytest

from app import computer
from app.computer import desktop

pytestmark = pytest.mark.skipif(
    not os.environ.get("GM_LIVE_DESKTOP"),
    reason="真机测试会打开记事本；设 GM_LIVE_DESKTOP=1 才跑")


@pytest.fixture(scope="module")
def notepad(tmp_path_factory):
    # 用唯一文件名开一个**新的**记事本：标题里带随机串，避免误碰用户已经开着的记事本
    path = tmp_path_factory.mktemp("cu") / ("cu_test_%s.txt" % os.urandom(4).hex())
    path.write_text("", encoding="utf-8")
    tag = path.stem
    proc = subprocess.Popen(["notepad.exe", str(path)])
    win = None
    deadline = time.time() + 12
    while time.time() < deadline and win is None:
        time.sleep(0.5)
        root = desktop._uia().GetRootControl()
        for w in root.GetChildren():
            try:
                if tag in (w.Name or ""):
                    win = w
                    break
            except Exception:
                pass
    if win is None:
        proc.terminate()
        pytest.skip("没等到记事本窗口")
    yield {"tag": tag, "pid": win.ProcessId, "proc": proc}
    # 只关我们自己开的那个（标题里带 tag）
    try:
        subprocess.run(["taskkill", "/PID", str(win.ProcessId), "/F"], capture_output=True)
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass


def test_notepad_end_to_end(notepad, monkeypatch):
    monkeypatch.setattr(desktop, "enabled", lambda: True)
    monkeypatch.setattr(desktop, "allowed_exes", lambda: ["notepad.exe"])
    monkeypatch.setattr(computer, "enabled", lambda: True)

    listing = computer.dispatch("computer_windows", {})
    assert notepad["tag"] in listing, listing
    key = ""
    for k, v in desktop._WINDOWS.items():
        if notepad["tag"] in (v.get("title") or ""):
            key = k
            break
    assert key, listing
    assert notepad["tag"] in desktop.window_title(key)   # 再确认一次，防抖

    snap = computer.dispatch("computer_see", {"target": key}, "")
    assert "文本编辑器" in snap or "Document" in snap or "Edit" in snap, snap

    m = re.search(r"\[(\d+)\] (Document|Edit) 「([^」]*)」", snap)
    assert m, snap
    ref = int(m.group(1))

    out = computer.dispatch("computer_act",
                            {"target": key, "action": "type", "ref": ref,
                             "value": "你好 CU"}, "")
    assert "已输入" in out, out
    time.sleep(0.3)
    val = computer.dispatch("computer_read",
                            {"target": key, "kind": "value", "ref": ref}, "")
    assert "你好 CU" in val, val

    trace = computer.dispatch("computer_read", {"kind": "trace"}, "")
    assert "computer_act" in trace
