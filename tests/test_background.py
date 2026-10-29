# -*- coding: utf-8 -*-
"""后台运行：窗口关闭后的任务保活。"""
import main


def test_keep_alive_waits_until_streams_finish(monkeypatch):
    """keep_alive=True：有任务时不按分钟数退出，等最后一个任务结束。"""
    seq = iter([2, 1, 0])
    monkeypatch.setattr(main, "_active_stream_count", lambda: next(seq))
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    monkeypatch.setattr(main, "_log", lambda *_a, **_k: None)
    main._wait_streams_grace(0, keep_alive=True)


def test_grace_zero_still_immediate_when_disabled(monkeypatch):
    """keep_alive=False 且 minutes=0：保持旧行为，立即返回、不查任务。"""
    called = []
    monkeypatch.setattr(main, "_active_stream_count",
                        lambda: called.append(1) or 0)
    main._wait_streams_grace(0, keep_alive=False)
    assert not called


def test_grace_timeout_when_disabled(monkeypatch):
    """keep_alive=False：到分钟数就返回，不会无限等。"""
    ticks = {"n": 0}
    monkeypatch.setattr(main, "_active_stream_count", lambda: 1)
    monkeypatch.setattr(main, "_log", lambda *_a, **_k: None)

    def fake_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 100:
            raise AssertionError("grace 没有按时退出")

    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    monkeypatch.setattr(main.time, "monotonic",
                        lambda: 0.0 if ticks["n"] < 2 else 999999.0)
    main._wait_streams_grace(0.1, keep_alive=False)
    assert ticks["n"] >= 1
