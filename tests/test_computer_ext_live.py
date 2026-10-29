# -*- coding: utf-8 -*-
"""Computer Use · 扩展桥真机联调（会启动 Edge + 加载 browser_ext）

默认跳过：
    set GM_LIVE_EXT=1 && python -m pytest tests/test_computer_ext_live.py -q

流程：起应用服务 → 起静态测试页 → Edge 加载扩展 → 打开扩展设置页填连接码
     → 通过扩展新建标签页 → 快照 → 点击 → 验证页面变化。
"""
import http.server
import json
import os
import re
import socketserver
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote  # noqa: F401  （保留：以后可能用 /json/new）

import pytest
import requests

from app import config as config_mod
from app import computer, httpd
from app.computer import bridge, browser, policy
from app.computer.cdp import CDP

pytestmark = pytest.mark.skipif(
    not os.environ.get("GM_LIVE_EXT"),
    reason="真机测试会启动 Edge 并加载扩展；设 GM_LIVE_EXT=1 才跑")

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EXT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "browser_ext")
TEST_KEY = "testkey-123"
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>扩展桥测试页</title></head>
<body><h1>扩展桥测试页</h1>
<button id="btn" onclick="document.getElementById('out').textContent='已点击'">点我</button>
<div id="out">初始</div></body></html>"""


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def test_extension_bridge_end_to_end():
    from app import webprobe
    if not os.path.isfile(EDGE) or not webprobe.edge_path():
        pytest.skip("本机没有 Edge")

    mp = pytest.MonkeyPatch()
    srv = site = proc = None
    work = tempfile.mkdtemp(prefix="cu_ext_")
    old_cwd = os.getcwd()
    try:
        # 1. 应用服务（桥端点）
        srv, app_port = httpd.create_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        # 2. 静态页
        page = os.path.join(work, "page.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(PAGE)
        os.chdir(work)
        site = socketserver.TCPServer(("127.0.0.1", 0), _Quiet)
        site_port = site.server_address[1]
        threading.Thread(target=site.serve_forever, daemon=True).start()

        # 3. 进程内配置（不写 config.json）
        cfg = {"computer_enabled": True, "computer_backend": "extension",
               "computer_sites": ["127.0.0.1"], "computer_extra_block": [],
               "computer_apps": [], "computer_snapshot_chars": 4000,
               "computer_auto_shot": False, "computer_bridge_key": TEST_KEY}
        mp.setattr(config_mod, "load_config", lambda: dict(cfg))
        mp.setattr(bridge, "key", lambda: TEST_KEY)
        mp.setattr(computer, "enabled", lambda: True)
        bridge.reset()

        # 4. Edge + 扩展
        prof = tempfile.mkdtemp(prefix="cu_ext_prof_")
        edge_port = 9446
        proc = subprocess.Popen([EDGE, "--remote-debugging-port=%d" % edge_port,
                                 "--user-data-dir=" + prof, "--no-first-run",
                                 "--no-default-browser-check",
                                 "--load-extension=" + EXT_DIR, "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                requests.get("http://127.0.0.1:%d/json/version" % edge_port, timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        time.sleep(2.0)
        targets = requests.get("http://127.0.0.1:%d/json/list" % edge_port, timeout=3).json()
        sw = [t for t in targets if (t.get("url") or "").endswith("/background.js")]
        assert sw, "扩展没有加载"
        ext_id = sw[0]["url"].split("/")[2]

        # 5. 设置页填连接码
        blank = [x for x in targets if x.get("type") == "page"][0]
        cdp = CDP(blank["webSocketDebuggerUrl"])
        cdp.send("Runtime.enable")
        cdp.send("Page.enable")
        cdp.send("Page.navigate", {"url": "chrome-extension://%s/options.html" % ext_id},
                 timeout=10)
        time.sleep(1.2)
        pair = "http://127.0.0.1:%d#%s" % (app_port, TEST_KEY)
        cdp.send("Runtime.evaluate", {
            "expression": "document.getElementById('pair').value=%s;"
                          "document.getElementById('save').click();'ok'" % json.dumps(pair),
            "returnByValue": True})

        deadline = time.time() + 20
        while time.time() < deadline and not bridge.available():
            time.sleep(0.3)
        assert bridge.available(), "扩展没有连上桥（检查连接码与 127.0.0.1 权限）"

        # 6. 打开 → 快照 → 点击
        url = "http://127.0.0.1:%d/page.html" % site_port
        text = computer.dispatch("computer_open", {"url": url}, "")
        assert "已打开" in text, text
        snap = computer.dispatch("computer_see", {}, "")
        assert "点我" in snap, snap
        ref = int(re.search(r"\[(\d+)\] button", snap).group(1))
        out = computer.dispatch("computer_act", {"action": "click", "ref": ref}, "")
        assert "已点击" in out, out
        assert "已点击" in computer.dispatch("computer_see", {}, "")
    finally:
        try:
            browser.close_all()
        except Exception:
            pass
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            time.sleep(0.4)
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"], capture_output=True)
        for s in (site, srv):
            try:
                s.shutdown()
            except Exception:
                pass
        os.chdir(old_cwd)
        mp.undo()
