# -*- coding: utf-8 -*-
"""按消息位置分叉（存档功能删除后仅保留的分支能力）。"""
from app import branch


def _sess():
    return {
        "id": "s1", "title": "对话", "updated_at": 1,
        "messages": [
            {"role": "user", "content": "一"},
            {"role": "assistant", "content": "二"},
            {"role": "user", "content": "三"},
        ],
        "summary": "", "summary_upto": 0,
    }


def test_branch_at_slices_messages():
    data = {"current": "s1", "sessions": [_sess()]}
    new_sid, err = branch.branch_at(data, data["sessions"][0], 2)
    assert err == ""
    assert new_sid and data["current"] == new_sid
    new = data["sessions"][0]
    assert new["id"] == new_sid
    assert len(new["messages"]) == 2
    assert new["messages"][1]["content"] == "二"
    assert new["title"].startswith("对话 · 分支")
    # 原会话不动
    assert len(data["sessions"][1]["messages"]) == 3


def test_branch_at_rejects_bad_upto():
    data = {"sessions": [_sess()]}
    s = data["sessions"][0]
    assert branch.branch_at(data, s, 0)[0] is None
    assert branch.branch_at(data, s, "x")[0] is None
    assert branch.branch_at(data, s, 999)[0] is not None   # 超界按总长度处理


def test_branch_at_clears_summary_beyond_cut():
    s = _sess()
    s["summary"] = "覆盖三条消息的摘要"
    s["summary_upto"] = 3
    data = {"sessions": [s]}
    new_sid, _ = branch.branch_at(data, s, 1)
    new = data["sessions"][0]
    assert new["id"] == new_sid
    assert new["summary"] == ""
    assert new["summary_upto"] == 0
