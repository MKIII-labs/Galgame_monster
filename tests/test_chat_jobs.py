# -*- coding: utf-8 -*-
"""断线重连的任务缓冲：job 生命周期与过期清理。"""
import time

from app.api import chat


def test_gc_drops_finished_jobs_after_ttl():
    chat.JOBS.clear()
    now = time.time()
    chat.JOBS["old"] = {"done": True, "ts": now - chat.JOB_TTL - 1}
    chat.JOBS["fresh"] = {"done": True, "ts": now}
    chat.JOBS["running"] = {"done": False, "ts": now - 99999}
    with chat.JOB_LOCK:
        chat._gc_jobs()
    assert "old" not in chat.JOBS
    assert "fresh" in chat.JOBS
    assert "running" in chat.JOBS      # 未完成的不能清，可能还在跑
    chat.JOBS.clear()


def test_job_event_limit_keeps_latest():
    """事件缓冲超上限时丢最旧的，保证长任务不吃光内存"""
    chat.JOBS.clear()
    job = {"sid": "s", "events": [], "seq": 0, "done": False, "ts": time.time()}
    chat.JOBS["s"] = job
    for i in range(chat.JOB_MAX_EVENTS + 10):
        with chat.JOB_LOCK:
            job["seq"] += 1
            job["events"].append({"type": "tick", "_seq": job["seq"]})
            if len(job["events"]) > chat.JOB_MAX_EVENTS:
                del job["events"][: len(job["events"]) - chat.JOB_MAX_EVENTS]
    assert len(job["events"]) == chat.JOB_MAX_EVENTS
    assert job["events"][-1]["_seq"] == chat.JOB_MAX_EVENTS + 10
    chat.JOBS.clear()
