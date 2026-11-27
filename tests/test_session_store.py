# -*- coding: utf-8 -*-
"""会话存储：每会话 append-only JSONL 是事实源，JSON 缓存可删可重建。

- `data/sessions/<id>/events.jsonl`：唯一事实源，只追加、永久保留；
- `data/session_store/<id>.json`：物化读缓存，丢了能从事件流重建；
- `data/sessions/index.json`：会话顺序与 current，丢了能按缓存扫描重建；
- 旧 `data/sessions.json`：首次加载时迁移并删除。
"""
import json
import os

from app import session_events as se
from app import sessions


def _sess(sid, title="会话", ts=1.0, msgs=None):
    return {"id": sid, "title": title, "updated_at": ts,
            "messages": msgs if msgs is not None else []}


def _read_index():
    with open(sessions._index_path(), encoding="utf-8") as f:
        return json.load(f)


def _restart():
    """模拟进程重启：丢掉内存副本与事件 diff 基线。"""
    sessions.reset_cache()
    se._LAST.clear()


def test_legacy_json_is_migrated_and_removed():
    sid = "s_old"
    data = {"current": sid, "saved_at": 10.0,
            "sessions": [_sess(sid, "旧会话", 10.0, [{"role": "user", "content": "hi"}])]}
    with open(sessions.SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    got = sessions.load_sessions()
    assert [s["id"] for s in got["sessions"]] == [sid]
    assert not os.path.exists(sessions.SESSION_FILE)      # 迁移后删除
    assert os.path.isfile(sessions._store_path(sid))      # 物化缓存
    assert _read_index()["order"] == [sid]                # 索引
    assert se.rebuild(sid)["messages"][0]["content"] == "hi"   # 事件流基线


def test_hot_save_only_touches_that_session():
    a = _sess("s_a", "A", 1.0, [{"role": "user", "content": "a"}])
    b = _sess("s_b", "B", 1.0, [{"role": "user", "content": "b"}])
    sessions.save_sessions({"current": "s_a", "sessions": [a, b]})
    with open(sessions._store_path("s_b"), "rb") as f:
        before = f.read()

    a["messages"].append({"role": "assistant", "content": "答案"})
    a["updated_at"] = 2.0
    sessions.save_session(a)

    with open(sessions._store_path("s_b"), "rb") as f:
        assert f.read() == before                          # 另一条没被重写
    _restart()
    got = sessions.load_sessions()
    assert sessions.find_session(got, "s_a")["messages"][-1]["content"] == "答案"
    assert sessions.find_session(got, "s_b")["messages"][0]["content"] == "b"


def test_progress_save_skips_cache_and_rebuilds_from_events():
    """进度快照只追加事件：物化缓存保持旧内容，重启后按事件流恢复最新进度。"""
    s = _sess("s_a", "A", 1.0, [{"role": "user", "content": "任务"}])
    sessions.save_sessions({"current": "s_a", "sessions": [s]})
    with open(sessions._store_path("s_a"), "rb") as f:
        before = f.read()

    s["messages"].append({"role": "assistant", "content": "半截", "_progress": True})
    s["updated_at"] = 2.0
    sessions.save_session(s, cache=False)

    with open(sessions._store_path("s_a"), "rb") as f:
        assert f.read() == before                       # 没有重写物化缓存
    _restart()
    got = sessions.load_sessions()
    assert sessions.find_session(got, "s_a")["messages"][-1]["content"] == "半截"


def test_deleted_session_does_not_revive():
    sessions.save_sessions({"current": "s_a",
                            "sessions": [_sess("s_a"), _sess("s_b")]})
    sessions.drop_session_files("s_b")
    _restart()
    got = sessions.load_sessions()
    assert [s["id"] for s in got["sessions"]] == ["s_a"]
    assert _read_index()["order"] == ["s_a"]


def test_cache_missing_rebuilds_from_events():
    s = _sess("s_a", "A", 1.0, [{"role": "user", "content": "存档"}])
    sessions.save_sessions({"current": "s_a", "sessions": [s]})
    os.remove(sessions._store_path("s_a"))                 # 缓存丢了
    _restart()
    got = sessions.load_sessions()
    assert sessions.find_session(got, "s_a")["messages"][0]["content"] == "存档"


def test_events_newer_than_cache_win():
    """事件写完、缓存没来得及写就退出：启动时应以事件流为准。"""
    s = _sess("s_a", "旧", 1.0, [{"role": "user", "content": "旧"}])
    sessions.save_sessions({"current": "s_a", "sessions": [s]})
    newer = _sess("s_a", "新", 2.0, [{"role": "user", "content": "新"}])
    se.append_baseline(newer)                              # 事件流更新
    _restart()
    got = sessions.load_sessions()
    assert sessions.find_session(got, "s_a")["title"] == "新"


def test_index_missing_rebuilds_order_from_cache():
    sessions.save_sessions({"current": "s_a",
                            "sessions": [_sess("s_a", "A", 2.0), _sess("s_b", "B", 1.0)]})
    os.remove(sessions._index_path())
    _restart()
    got = sessions.load_sessions()
    assert [s["id"] for s in got["sessions"]] == ["s_a", "s_b"]   # 按 updated_at 倒序
    assert os.path.isfile(sessions._index_path())


def test_stray_cache_is_merged_back():
    """索引被误覆盖成 1 条时，磁盘上还有的缓存必须自动并回来（防数据「消失」）。"""
    sessions.save_sessions({"current": "s_a",
                            "sessions": [_sess("s_a", "A", 2.0), _sess("s_b", "B", 1.0)]})
    sessions._write_index({"current": "s_a", "sessions": [_sess("s_a", "A", 2.0)]})
    _restart()
    got = sessions.load_sessions()
    assert [s["id"] for s in got["sessions"]] == ["s_a", "s_b"]
    assert _read_index()["order"] == ["s_a", "s_b"]


def test_tombstoned_session_is_not_revived():
    """删除会话会写墓碑：即使缓存没删干净，也不能被「残留扫描」复活。"""
    sessions.save_sessions({"current": "s_a", "sessions": [_sess("s_a"), _sess("s_b")]})
    sessions.drop_session_files("s_b")
    sessions._write_cache(_sess("s_b", "B", 1.0))        # 模拟残留缓存
    _restart()
    got = sessions.load_sessions()
    assert [s["id"] for s in got["sessions"]] == ["s_a"]


def test_load_sessions_returns_isolated_copy():
    sessions.save_sessions({"current": "s_a", "sessions": [_sess("s_a", "原标题")]})
    d = sessions.load_sessions()
    d["sessions"][0]["title"] = "改过"
    d["sessions"][0]["messages"].append({"role": "user", "content": "未保存"})
    again = sessions.load_sessions()
    assert again["sessions"][0]["title"] == "原标题"
    assert again["sessions"][0]["messages"] == []


def test_cache_sessions_is_memory_only():
    sessions.save_sessions({"current": "s_a", "sessions": [_sess("s_a", "A")]})
    d = sessions.load_sessions()
    d["sessions"][0]["title"] = "立刻可见"
    sessions.cache_sessions(d)
    assert sessions.load_sessions()["sessions"][0]["title"] == "立刻可见"
    _restart()
    assert sessions.load_sessions()["sessions"][0]["title"] == "A"   # 没落盘


def test_save_sessions_returns_bool():
    assert sessions.save_sessions({"current": "s_x", "sessions": [_sess("s_x")]}) is True
    assert sessions.save_sessions(None) is False


def test_storage_stats_counts_cache_and_orphans():
    sessions.save_sessions({"current": "s_a",
                            "sessions": [_sess("s_a", "A", 1.0), _sess("s_b", "B", 1.0)]})
    # 模拟一个已删除会话留下的孤儿事件目录
    ghost = os.path.join(str(sessions._sessions_root()), "s_ghost")
    os.makedirs(ghost, exist_ok=True)
    with open(os.path.join(ghost, "events.jsonl"), "w", encoding="utf-8") as f:
        f.write("{}\n")
    sessions.reset_cache()
    st = sessions.storage_stats(ttl=0)
    assert st["sessions"] == 2
    assert st["cache_bytes"] > 0
    assert st["event_bytes"] >= 2
    assert st["largest_event_bytes"] >= 2
    assert st["event_warn"] is False
    assert st["orphan_dirs"] >= 1


def test_slim_message_normalizes_sources_without_mutating_original():
    srcs = [
        {"title": "A", "url": "https://a.example"},
        {"title": "A again", "url": "https://a.example"},
    ]
    m = {"role": "assistant", "content": "见 [1]",
         "sources": srcs, "search": {"hits": 9, "pages": []}}
    out = sessions.slim_message_for_client(m)
    assert [s["url"] for s in out["sources"]] == ["https://a.example"]
    assert out["sources"][0]["n"] == 1
    assert out["search"]["hits"] == 1
    # 原对象与来源字典都不能被改（读缓存共享）
    assert len(m["sources"]) == 2
    assert "n" not in srcs[0]
    assert m["search"]["hits"] == 9


def test_normalize_session_search_skips_out_of_range_citations():
    in_range = {"role": "assistant", "content": "见 [2]",
                "sources": [{"title": "A", "url": "a"}, {"title": "B", "url": "b"}]}
    out_range = {"role": "assistant", "content": "见 [9]",
                 "sources": [{"title": "C", "url": "c"}]}
    data = {"sessions": [{"id": "s", "messages": [in_range, out_range]}]}
    assert sessions.normalize_session_search(data) is True
    assert [s["n"] for s in in_range["sources"]] == [1, 2]
    assert "n" not in out_range["sources"][0]


def test_slim_message_for_share_drops_heavy_fields():
    m = {"role": "assistant", "content": "正文", "blocks": [{"kind": "text"}],
         "tools": [{"tool": "x"}], "timing": {"total_ms": 1}, "plan": {"steps": []},
         "usage": {"prompt": 1}, "memory": {"ops": []}, "reasoning": "think",
         "end_reason": {"kind": "done"},
         "sources": [{"title": "A", "url": "https://a.example"}]}
    out = sessions.slim_message_for_client(m, slim=True)
    for key in ("blocks", "tools", "timing", "plan", "usage", "memory",
                "reasoning", "end_reason"):
        assert key not in out
    assert out["content"] == "正文"
    assert out["sources"][0]["url"] == "https://a.example"
