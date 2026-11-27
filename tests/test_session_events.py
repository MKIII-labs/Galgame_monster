# -*- coding: utf-8 -*-
"""append-only 会话事件日志：diff 粒度 / 重建一致性 / 无变化不追加。"""
import json
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


def test_last_snapshot_is_capped(monkeypatch):
    """_LAST 不能随会话数无限增长：超过上限时按最近使用淘汰。"""
    monkeypatch.setattr(se, "LAST_CAP", 2)
    se._LAST.clear()
    for sid in ("a", "b", "c"):
        se.seed_last({"id": sid, "messages": []})
    assert list(se._LAST) == ["b", "c"]


def test_reasoning_blocks_use_incremental_events(tmp_path):
    """思考变长只记增量，不能每次进度落库都整段重写 blocks。"""
    se._LAST.clear()
    text = "想" * 2000
    msg = {"role": "assistant", "content": "", "blocks": [{"kind": "reasoning", "text": text}]}
    s = _sess([msg])
    se.record(s, str(tmp_path))                       # 基线
    p = se.path_of("t1", str(tmp_path))

    msg["blocks"][0]["text"] = text + "，又想了 2000 字" * 200
    se.record(s, str(tmp_path))
    with open(p, encoding="utf-8") as f:
        appended = [json.loads(x) for x in f.read().splitlines()[1:]]
    assert [e["type"] for e in appended] == ["message/append_block_text"]
    assert len(appended[0]["text"]) == len(msg["blocks"][0]["text"]) - len(text)

    got = se.rebuild("t1", str(tmp_path))
    assert got["messages"][0]["blocks"][0]["text"] == msg["blocks"][0]["text"]

    # 新增一个块：追加块事件
    msg["blocks"].append({"kind": "text", "text": "答案"})
    se.record(s, str(tmp_path))
    got = se.rebuild("t1", str(tmp_path))
    assert [b["kind"] for b in got["messages"][0]["blocks"]] == ["reasoning", "text"]


def test_rotation_keeps_history(tmp_path, monkeypatch):
    """超过 MAX_BYTES 滚动 .1.jsonl；重建仍能读到滚动前后的全部事件。"""
    se._LAST.clear()
    monkeypatch.setattr(se, "MAX_BYTES", 1)        # 任何写入后都触发下一次滚动
    s = _sess([{"role": "user", "content": "第一句"}])
    se.record(s, str(tmp_path))
    s2 = _sess([{"role": "user", "content": "第一句"},
                {"role": "assistant", "content": "第二句"}])
    s2["updated_at"] = 2.0
    se.record(s2, str(tmp_path))
    path = os.path.join(str(tmp_path), "sessions", "t1", "events.jsonl")
    assert os.path.isfile(path[:-len(".jsonl")] + ".1.jsonl")
    got = se.rebuild("t1", str(tmp_path))
    assert [m["content"] for m in got["messages"]] == ["第一句", "第二句"]
    se._LAST.clear()
