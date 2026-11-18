# -*- coding: utf-8 -*-
"""事件日志清理：保留快照基线 + 增量，历史事件可回收。"""
import json
import os

from app import session_event_prune as sep
from app import session_events as se


def _write(path, events):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _sessions_file(tmp_path, saved_at, sessions):
    p = os.path.join(tmp_path, "sessions.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"saved_at": saved_at, "sessions": sessions}, f, ensure_ascii=False)
    return p


def _data(tmp_path, saved_at, sessions):
    p = _sessions_file(tmp_path, saved_at, sessions)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def test_covered_events_become_baseline(tmp_path):
    sid = "t1"
    big = "x" * 70000                     # 撑过 MIN_BYTES
    snap = {"id": sid, "title": "会话", "updated_at": 900.0,
            "messages": [{"role": "user", "content": big},
                         {"role": "assistant", "content": "答"}]}
    data = _data(tmp_path, 1000.0, [snap])
    path = se.path_of(sid, str(tmp_path))
    _write(path, [
        {"ts": 500.0, "seq": 1, "type": "session/created", "session": {"id": sid}},
        # 历史流式事件的正文本身就很大（真实场景：长回答 / 长附件）
        {"ts": 501.0, "seq": 2, "type": "message/append", "index": 0,
         "message": {"role": "user", "content": big}},
        {"ts": 502.0, "seq": 3, "type": "message/append", "index": 1,
         "message": {"role": "assistant", "content": "答"}},
    ])
    rotated = path[:-len(".jsonl")] + ".1.jsonl"
    # 滚动文件里是最早的流式正文：早已被快照覆盖，体积远大于快照本身
    _write(rotated, [
        {"ts": 499.0, "seq": 0, "type": "session/created", "session": {"id": sid}},
        {"ts": 499.5, "seq": 1, "type": "message/append", "index": 0,
         "message": {"role": "user", "content": "z" * 200000}},
    ])
    before = os.path.getsize(path) + os.path.getsize(rotated)

    todo, skipped, stats = sep.plan(data, str(tmp_path))
    assert [r["sid"] for r in todo] == [sid]
    assert stats["tail"] == 0
    assert skipped == []
    assert todo[0]["after"] < before

    backup = os.path.join(str(tmp_path), "backup")
    assert sep.apply_row(todo[0], backup)
    assert not os.path.exists(rotated)
    assert os.path.getsize(path) < before
    assert len(os.listdir(backup)) == 2          # 原文件都备份了

    got = se.rebuild(sid, str(tmp_path))
    assert [m["content"] for m in got["messages"]] == [big, "答"]
    assert se.latest_ts(sid, str(tmp_path)) == 0  # 基线不算「比快照新」


def test_newer_tail_is_kept(tmp_path):
    sid = "t2"
    big = "y" * 70000
    snap = {"id": sid, "title": "会话2", "updated_at": 900.0,
            "messages": [{"role": "user", "content": big}]}
    data = _data(tmp_path, 1000.0, [snap])
    path = se.path_of(sid, str(tmp_path))
    _write(path, [
        {"ts": 500.0, "seq": 1, "type": "session/created", "session": {"id": sid}},
        {"ts": 501.0, "seq": 2, "type": "message/append", "index": 0,
         "message": {"role": "user", "content": big}},
        {"ts": 1500.0, "seq": 3, "type": "message/append", "index": 1,
         "message": {"role": "assistant", "content": "快照之后"}},
    ])
    # 滚动文件里的历史正文已进快照，清理时应当一起回收
    rotated = path[:-len(".jsonl")] + ".1.jsonl"
    _write(rotated, [{"ts": 400.0, "seq": 0, "type": "message/append", "index": 0,
                      "message": {"role": "user", "content": "z" * 200000}}])
    todo, _, stats = sep.plan(data, str(tmp_path))
    assert stats["tail"] == 1
    assert sep.apply_row(todo[0], os.path.join(str(tmp_path), "bak"))
    got = se.rebuild(sid, str(tmp_path))
    assert [m["content"] for m in got["messages"]] == [big, "快照之后"]
    assert se.latest_ts(sid, str(tmp_path)) == 1500.0


def test_small_or_unknown_sessions_untouched(tmp_path):
    sid = "t3"
    snap = {"id": sid, "title": "小", "messages": [{"role": "user", "content": "hi"}]}
    data = _data(tmp_path, 1000.0, [snap])
    _write(se.path_of(sid, str(tmp_path)),
           [{"ts": 1.0, "seq": 1, "type": "session/created", "session": {"id": sid}}])
    _write(se.path_of("ghost", str(tmp_path)),
           [{"ts": 1.0, "seq": 1, "type": "session/created", "session": {"id": "ghost"}}])
    todo, skipped, _ = sep.plan(data, str(tmp_path))
    assert todo == [] and skipped == []


def test_orphan_dirs_only_removed_when_older(tmp_path):
    data = _data(tmp_path, 1000.0, [])
    old = se.path_of("old", str(tmp_path))
    _write(old, [{"ts": 900.0, "seq": 1, "type": "session/created",
                  "session": {"id": "old"}}])
    new = se.path_of("new", str(tmp_path))
    _write(new, [{"ts": 1100.0, "seq": 1, "type": "session/created",
                  "session": {"id": "new"}}])
    todo, keep = sep.scan_orphans(data, str(tmp_path))
    assert [r["sid"] for r in todo] == ["old"]
    assert [r["sid"] for r in keep] == ["new"]

    backup = os.path.join(str(tmp_path), "bak")
    assert sep.apply_orphan(todo[0], backup)
    assert not os.path.exists(os.path.dirname(old))
    assert os.path.isdir(os.path.join(backup, "old"))
    assert os.path.isdir(os.path.dirname(new))
