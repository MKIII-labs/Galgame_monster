# -*- coding: utf-8 -*-
"""计划审批（Claude 式 plan mode）：逐条改步骤、挂起 / 回答 / 超时。"""
import threading
import time

from app import agent, session_status
from app.api import chat


def _plan():
    return agent.normalize_plan({"goal": "做个页面", "steps": [
        {"id": "S1", "text": "写 HTML"},
        {"id": "S2", "text": "写样式", "verify": "打开看效果"},
    ]})[0]


def test_apply_plan_edits_keeps_ids_and_bumps_revision():
    plan = _plan()
    new, err = chat._apply_plan_edits(plan, [
        {"id": "S1", "text": "先写结构"},
        {"id": "S2", "text": "再写样式", "status": "pending"},
    ])
    assert err == "" and new
    assert [s["text"] for s in new["steps"]] == ["先写结构", "再写样式"]
    assert new["steps"][1]["verify"] == "打开看效果"
    assert int(new["revision"]) == int(plan["revision"]) + 1


def test_apply_plan_edits_rejects_empty():
    plan = _plan()
    assert chat._apply_plan_edits(plan, [])[0] is None
    assert chat._apply_plan_edits(plan, [{"text": "  "}])[0] is None


def test_plan_review_roundtrip():
    sid = "s_test_plan_review"
    rid = agent.new_plan_review(sid, _plan())
    assert session_status.snapshot().get(sid) == "approval"

    def _answer():
        deadline = time.time() + 2
        while time.time() < deadline:
            if agent.resolve_plan_review(rid, "comment", "步骤太粗", None):
                return
            time.sleep(0.02)

    th = threading.Thread(target=_answer)
    th.start()
    res = agent.wait_plan_review(rid, timeout=5)
    th.join()
    assert res and res["action"] == "comment" and "太粗" in res["comment"]
    assert agent.get_plan_review(rid) is None
    assert sid not in session_status.snapshot()


def test_plan_review_rejects_second_answer():
    sid = "s_test_plan_review2"
    rid = agent.new_plan_review(sid, _plan())
    assert agent.resolve_plan_review(rid, "approve") is True
    assert agent.resolve_plan_review(rid, "comment", "晚了一步") is False
    res = agent.wait_plan_review(rid, timeout=2)
    assert res and res["action"] == "approve"
    assert agent.resolve_plan_review("pr_missing", "approve") is False


def test_plan_review_timeout_returns_none():
    sid = "s_test_plan_review3"
    rid = agent.new_plan_review(sid, _plan())
    assert agent.wait_plan_review(rid, timeout=0.05) is None
    assert sid not in session_status.snapshot()
