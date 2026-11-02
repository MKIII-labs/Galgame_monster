# -*- coding: utf-8 -*-
"""后台运行：窗口关闭后的任务保活。

保活口径 = 对话回合（ACTIVE_STREAMS）+ 后台长任务（子代理批次 / 自动化线程）。
"""
import main


def _no_bg(monkeypatch):
    """单元测试里默认没有后台任务，避免受别的用例残留状态影响。"""
    monkeypatch.setattr(main, "_background_job_count", lambda: 0)


def test_keep_alive_waits_until_streams_finish(monkeypatch):
    """keep_alive=True：有任务时不按分钟数退出，等最后一个任务结束。"""
    _no_bg(monkeypatch)
    seq = iter([2, 1, 0])
    monkeypatch.setattr(main, "_active_stream_count", lambda: next(seq))
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    monkeypatch.setattr(main, "_log", lambda *_a, **_k: None)
    main._wait_streams_grace(0, keep_alive=True)


def test_keep_alive_waits_for_background_jobs(monkeypatch):
    """没有对话流、只有后台批次 / 自动化时，也要等它们跑完。"""
    seq = iter([2, 1, 0])
    monkeypatch.setattr(main, "_active_stream_count", lambda: 0)
    monkeypatch.setattr(main, "_background_job_count", lambda: next(seq))
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    monkeypatch.setattr(main, "_log", lambda *_a, **_k: None)
    main._wait_streams_grace(0, keep_alive=True)


def test_pending_job_count_sums_streams_and_background(monkeypatch):
    monkeypatch.setattr(main, "_active_stream_count", lambda: 1)
    monkeypatch.setattr(main, "_background_job_count", lambda: 2)
    assert main._pending_job_count() == 3


def test_grace_zero_still_immediate_when_disabled(monkeypatch):
    _no_bg(monkeypatch)
    called = []
    monkeypatch.setattr(main, "_active_stream_count",
                        lambda: called.append(1) or 0)
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    main._wait_streams_grace(0, keep_alive=False)
    assert not called


def test_grace_timeout_when_disabled(monkeypatch):
    _no_bg(monkeypatch)
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


def test_autopilot_running_count_counts_live_threads(monkeypatch):
    from app import autopilot

    class T:
        def __init__(self, alive):
            self._alive = alive

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(autopilot, "_THREADS", {"a": T(True), "b": T(False)})
    assert autopilot.running_count() == 1


def test_explore_running_count_counts_running_only(monkeypatch):
    from app import explore
    with explore._JOB_LOCK:
        saved = dict(explore._JOBS)
        explore._JOBS.clear()
        explore._JOBS["ex_00000001"] = {"status": "running"}
        explore._JOBS["ex_00000002"] = {"status": "done"}
    try:
        assert explore.running_count() == 1
    finally:
        with explore._JOB_LOCK:
            explore._JOBS.clear()
            explore._JOBS.update(saved)
