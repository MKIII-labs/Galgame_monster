# -*- coding: utf-8 -*-
"""Computer Use · 真机联调（会真的弹一个 Edge 窗口）

默认跳过，需要显式打开：
    set GM_LIVE_BROWSER=1 && python -m pytest tests/test_computer_live.py -q
"""
import os
import time

import pytest

from app import computer
from app.computer import browser, policy

pytestmark = pytest.mark.skipif(
    not os.environ.get("GM_LIVE_BROWSER"),
    reason="真机测试会启动浏览器窗口；设 GM_LIVE_BROWSER=1 才跑")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>探针页</title>
<style>body{font-family:sans-serif}#out{font-size:16px}</style></head><body>
<h1>计算机使用测试</h1>
<button id="btn" onclick="document.getElementById('out').textContent='已点击'">点我</button>
<input id="name" placeholder="名字">
<select id="pick"><option value="a">甲</option><option value="b">乙</option></select>
<div id="out">初始</div>
<script>
setTimeout(function(){ console.error('页面自己的报错'); }, 100);
</script>
</body></html>
"""


@pytest.fixture(scope="module", autouse=True)
def _allow_local(tmp_path_factory):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(computer, "enabled", lambda: True)
    monkeypatch.setattr(policy, "_cfg", lambda: {"computer_sites": [], "extra": []})
    # 用临时端口文件 + 临时 profile：每次都拉起自己的浏览器，结束后能干净关掉
    monkeypatch.setattr(browser, "PORT_FILE",
                        str(tmp_path_factory.mktemp("port") / "p.txt"))
    monkeypatch.setattr(browser, "PROFILE_DIR",
                        str(tmp_path_factory.mktemp("profile")))
    yield
    browser.shutdown()
    monkeypatch.undo()


def test_end_to_end(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(PAGE, encoding="utf-8")
    url = policy.url_of_file(page)
    policy.grant_session("")

    text = computer.dispatch("computer_open", {"url": url}, str(tmp_path))
    # file:// 在策略里是 ask：必须先授权。这里直接绕过审批层，验证浏览器链路本身。
    assert text.startswith("❌") or "已打开" in text or "未授权" in text

    # 直接导航（跳过策略层的 ask），只验证 CDP 能否工作
    browser.ensure()
    browser.navigate(url)
    snap = computer.see(limit=20)
    assert "计算机使用测试" in snap
    assert "点我" in snap

    m = __import__("re").search(r"\[(\d+)\] button\S* 「点我」", snap)
    assert m, snap
    ref = int(m.group(1))
    out = computer.act("click", ref)
    assert "已点击" in out
    snap2 = computer.see(limit=20)
    assert "已点击" in snap2

    m2 = __import__("re").search(r"\[(\d+)\] input", snap2)
    assert m2, snap2
    out = computer.act("type", int(m2.group(1)), "张三")
    assert "输入" in out
    assert "张三" in computer.read("value", int(m2.group(1)))

    time.sleep(0.3)
    logs = computer.read("console")
    assert "页面自己的报错" in logs

    m3 = __import__("re").search(r"\[(\d+)\] select", snap2)
    if m3:
        assert "甲" in computer.act("type", int(m3.group(1)), "b")

    assert "computer_act" in computer.read("trace")
