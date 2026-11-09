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
