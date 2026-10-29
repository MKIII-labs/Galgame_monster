# -*- coding: utf-8 -*-
"""任务预算：80% 提醒 / 100% 停止 / 0 关闭。"""
from app.api.chat import _budget_action


def test_budget_disabled():
    assert _budget_action(999, 0, False) == "allow"
    assert _budget_action(999, None, False) == "allow"


def test_budget_warns_at_80_percent():
    assert _budget_action(79, 100, False) == "allow"
    assert _budget_action(80, 100, False) == "warn"
    assert _budget_action(90, 100, True) == "allow"


def test_budget_stops_at_limit():
    assert _budget_action(100, 100, False) == "stop"
    assert _budget_action(120, 100, True) == "stop"
