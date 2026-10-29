# -*- coding: utf-8 -*-
"""append-only 会话事件日志：diff 粒度 / 重建一致性 / 无变化不追加。"""
import os

from app import session_events as se


def _sess(msgs=None):
    return {"id": "t1", "title": "测试", "updated_at": 1.0,
            "messages": msgs if msgs is not None else []}


def _types(evs):
    return [t for t, _ in evs]


def test_diff_new_session():
    s = _sess([{"role": "user", "content": "hi"}])
    evs = se.diff(None, s)
    assert _types(evs) == ["session/created", "message/append"]


def test_diff_append_content_and_tools():
    old = _sess([{"role": "assistant", "content": "半句", "tools": []}])
    new = _sess([{"role": "assistant", "content": "半句 + 新增",
                  "tools": [{"tool": "read_file", "args": {}, "result": "x"}]}])
    kinds = _types(se.diff(old, new))
    assert "message/append_content" in kinds
    assert "message/append_tools" in kinds


def test_diff_truncate_and_meta():
    old = _sess([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    new = _sess([{"role": "user", "content": "a"}])
    new["title"] = "改了名"
    evs = se.diff(old, new)
    assert _types(evs) == ["session/meta", "message/truncate"]


def test_record_and_rebuild_roundtrip(tmp_path):
    se._LAST.clear()
    s = _sess([{"role": "user", "content": "第一句"}])
    se.record(s, str(tmp_path))                       # 首次写一条完整基线
    got0 = se.rebuild("t1", str(tmp_path))
    assert [m["content"] for m in got0["messages"]] == ["第一句"]

    s["messages"].append({"role": "assistant", "content": "回答"})
    s["updated_at"] = 2.0
    se.record(s, str(tmp_path))
    got = se.rebuild("t1", str(tmp_path))
    assert [m["content"] for m in got["messages"]] == ["第一句", "回答"]

    # 流式追加正文 + 工具：走增量事件，重建结果必须与源一致
    s["messages"][1]["content"] += "，补充"
    s["messages"][1]["tools"] = [{"tool": "read_file"}]
    s["updated_at"] = 3.0
    se.record(s, str(tmp_path))
    got = se.rebuild("t1", str(tmp_path))
    assert got["messages"][1]["content"] == "回答，补充"
    assert got["messages"][1]["tools"] == [{"tool": "read_file"}]


def test_record_skips_unchanged(tmp_path):
    se._LAST.clear()
    s = _sess([{"role": "user", "content": "x"}])
    se.record(s, str(tmp_path))
    s["messages"].append({"role": "assistant", "content": "y"})
    s["updated_at"] = 2.0
    se.record(s, str(tmp_path))
    p = se.path_of("t1", str(tmp_path))
    before = os.path.getsize(p)
    se.record(s, str(tmp_path))                       # 完全相同：不追加
    assert os.path.getsize(p) == before
