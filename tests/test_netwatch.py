# -*- coding: utf-8 -*-
"""外联审计：进程树 / 连接采集 / 摘要格式（只记录，不拦截）。"""
import os

from app import netwatch


def test_summary_format():
    items = [{"exe": "python.exe", "ip": "1.2.3.4", "port": 443},
             {"exe": "", "ip": "5.6.7.8", "port": 80}]
    text = netwatch.summary(items)
    assert "1.2.3.4:443（python.exe）" in text
    assert "5.6.7.8:80" in text
    assert netwatch.summary([]) == ""


def test_summary_truncates_long_list():
    items = [{"exe": "x", "ip": "10.0.0.%d" % i, "port": 80} for i in range(10)]
    text = netwatch.summary(items, limit=3)
    assert "外部连接 10 个" in text and "等 10 个" in text


def test_process_tree_includes_self():
    if os.name != "nt":
        return
    tree = netwatch.process_tree(os.getpid())
    assert os.getpid() in tree


def test_watcher_stop_returns_list():
    if os.name != "nt":
        return
    w = netwatch.Watcher(os.getpid(), interval=0.2).start()
    items = w.stop()
    assert isinstance(items, list)
    for it in items:
        assert set(it) == {"exe", "ip", "port"}
