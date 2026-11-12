# -*- coding: utf-8 -*-
"""会话运行态：待审批 / 待回答的登记、消减与快照。"""
import pytest

import app.session_status as status


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """别的测试会调用 agent.new_approval / new_asks 留下全局登记，这里换一份空表。"""
    monkeypatch.setattr(status, "_pending", {})


def test_mark_and_clear():
    status.mark("s1", "approval")
    assert status.snapshot() == {"s1": "approval"}
    # 同一会话同类累加：清一次还在
    status.mark("s1", "approval")
    status.clear("s1", "approval")
    assert status.snapshot() == {"s1": "approval"}
    status.clear("s1", "approval")
    assert status.snapshot() == {}


def test_clear_wrong_kind_is_ignored():
    status.mark("s1", "ask")
    status.clear("s1", "approval")          # 类型不匹配：不能误清
    assert status.snapshot() == {"s1": "ask"}


def test_drop_clears_everything():
    status.mark("s1", "approval")
    status.mark("s1", "approval")
    status.drop("s1")
    assert status.snapshot() == {}


def test_event_carries_notice_text(monkeypatch):
    """待处理事件要带上给系统通知看的摘要与登记时间（前端据此去重、弹窗）。"""
    seen = []
    monkeypatch.setattr(status.events, "publish", lambda obj: seen.append(obj))

    status.mark("s1", "ask", "选哪个方案？")
    ev = seen[-1]
    assert ev["type"] == "session_pending"
    assert ev["kind"] == "ask" and ev["count"] == 1
    assert ev["text"] == "选哪个方案？" and ev["at"] > 0

    # 同类再登记：计数累加，摘要换成最新那条
    status.mark("s1", "ask", "换个问题")
    assert seen[-1]["count"] == 2 and seen[-1]["text"] == "换个问题"

    # 清到零后没有摘要：前端不会再弹通知（登记了两次，所以要清两次）
    status.clear("s1", "ask")
    assert seen[-1]["kind"] == "ask" and seen[-1]["text"] == "换个问题"
    status.clear("s1", "ask")
    assert seen[-1]["kind"] == "" and "text" not in seen[-1]


def test_invalid_input_is_ignored():
    status.mark("", "approval")
    status.mark("s1", "nope")
    assert status.snapshot() == {}


def test_stale_entries_expire(monkeypatch):
    status.mark("s1", "ask")
    now = status.time.time()
    monkeypatch.setattr(status.time, "time", lambda: now + status.TTL + 1)
    assert status.snapshot() == {}


def test_refresh_keeps_waiting_entry(monkeypatch):
    """长提问（最长 1 小时）每 30 秒续期，侧栏「待回答」不会被 TTL 提前清掉。"""
    status.mark("s1", "ask")
    now = status.time.time()
    monkeypatch.setattr(status.time, "time", lambda: now + status.TTL + 1)
    status.refresh("s1", "ask")
    assert status.snapshot() == {"s1": "ask"}
