# -*- coding: utf-8 -*-
"""存储维护：磁盘占用统计、事件日志压缩、孤儿目录清理。"""
import json
import os

from app import session_events as se
from app import sessions
from app import store
from app.api import storage as st


def _write(path, events):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


class _H:
    def __init__(self, body=None):
        self._incoming = body or {}
        self.obj = None
        self.code = 200

    def _body(self):
        return self._incoming

    def _json(self, payload, code=200):
        self.obj = payload
        self.code = code
        return payload


def test_routes_registered_and_usage_payload(tmp_path, monkeypatch):
    from app.registry import snapshot
    assert ("POST", "/api/storage/usage") in snapshot()
    assert ("POST", "/api/storage/maintenance") in snapshot()
    data = tmp_path / "data"
    (data / "sessions").mkdir(parents=True)
    (data / "sessions" / "a.bin").write_bytes(b"x" * 64)
    monkeypatch.setattr(store, "DATA_DIR", str(data))
    h = _H()
    st._api_storage_usage(h)
    assert h.obj["ok"] is True and h.obj["total"] >= 64
    names = [x["name"] for x in h.obj["dirs"]]
    assert "sessions" in names


def test_dir_size_counts_files(tmp_path):
    d = tmp_path / "x"
    (d / "a").mkdir(parents=True)
    (d / "a" / "1.txt").write_bytes(b"12345")
    (d / "2.txt").write_bytes(b"123")
    size, files, cut = st._dir_size(d)
    assert size == 8 and files == 2 and cut is False


def test_usage_lists_data_dirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "sessions").mkdir(parents=True)
    (data / "sessions" / "a.bin").write_bytes(b"x" * 100)
    (data / "undo").mkdir()
    monkeypatch.setattr(store, "DATA_DIR", str(data))
    rows = []
    total = 0
    for name in os.listdir(data):
        size, files, _ = st._dir_size(os.path.join(data, name))
        rows.append({"name": name, "bytes": size})
        total += size
    assert {"name": "sessions", "bytes": 100} in rows and total == 100


def test_prune_events_and_orphans_apply(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(store, "DATA_DIR", str(data))
    snap = {"id": "keep", "title": "保留", "updated_at": 900.0,
            "messages": [{"role": "user", "content": "最终一小段"}]}
    sessions.save_sessions({"current": "keep", "sessions": [snap]})
    path = se.path_of("keep", str(se.DATA_DIR))
    # 历史事件远大于快照（真实场景：长回答被反复追加），全部早于快照时间
    events = [{"ts": 500.0, "seq": 1, "type": "session/created", "session": {"id": "keep"}}]
    for i in range(30):
        events.append({"ts": 501.0 + i, "seq": i + 2, "type": "message/append",
                       "index": 0, "message": {"role": "user", "content": "y" * 10000}})
    _write(path, events)
    before = os.path.getsize(path)
    assert before > 64 * 1024
    # 已从会话索引删除的会话：最后一条事件早于 saved_at，可整体移走
    orphan = os.path.join(str(se.DATA_DIR), "sessions", "gone")
    _write(os.path.join(orphan, "events.jsonl"),
           [{"ts": 400.0, "seq": 1, "type": "session/created",
             "session": {"id": "gone"}}])

    preview = st._maintenance(apply=False)
    assert preview["events"]["candidates"] == 1
    assert preview["orphans"]["count"] == 1
    assert os.path.isdir(orphan)                # 预览不删东西

    out = st._maintenance(apply=True)
    assert out["done"] == 1 and out["moved"] == 1
    assert os.path.getsize(path) < before
    assert not os.path.isdir(orphan)
    assert out["backup"] and os.path.isdir(out["backup"])
    got = se.rebuild("keep", str(se.DATA_DIR))
    assert [m["content"] for m in got["messages"]] == ["最终一小段"]
