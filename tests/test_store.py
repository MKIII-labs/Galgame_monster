# -*- coding: utf-8 -*-
"""store：JSON 原子写 + mtime 缓存"""
import os

from app import store


def test_write_then_read(tmp_path):
    p = str(tmp_path / "a.json")
    store.write_json(p, {"x": 1})
    assert store.read_json(p) == {"x": 1}


def test_read_missing_returns_default(tmp_path):
    assert store.read_json(str(tmp_path / "nope.json"), {"d": 1}) == {"d": 1}
    assert store.read_json(str(tmp_path / "nope.json")) is None


def test_rewrite_is_visible_despite_cache(tmp_path):
    """mtime 缓存不能让「刚写进去的新内容」读成旧值"""
    p = str(tmp_path / "b.json")
    store.write_json(p, {"v": "old"})
    assert store.read_json(p)["v"] == "old"
    store.write_json(p, {"v": "new"})
    assert store.read_json(p)["v"] == "new"


def test_no_partial_file_on_disk(tmp_path):
    """写完后目录里不该残留 .tmp 半边文件"""
    p = str(tmp_path / "c.json")
    store.write_json(p, {"k": list(range(50))})
    leftovers = [f for f in tmp_path.iterdir() if f.name.endswith(".tmp")]
    assert leftovers == []


def test_write_json_remember_false_skips_cache(tmp_path):
    """写一次、极少读的大文件（会话存档快照）不该进读缓存，否则每轮都泄漏内存。"""
    p = str(tmp_path / "state.json")
    store.write_json(p, {"k": 1}, indent=None, remember=False)
    assert p not in store._json_cache
    assert store.read_json(p) == {"k": 1}        # 读的时候才进缓存
    store.drop_cache(p)
    assert p not in store._json_cache


def test_json_cache_is_bounded(tmp_path):
    """读缓存有上限：读一堆文件不会无限增长；sessions.json 钉住不被淘汰。"""
    old_max = store._JSON_CACHE_MAX
    store._JSON_CACHE_MAX = 8
    paths = []
    try:
        for i in range(20):
            p = str(tmp_path / ("f%d.json" % i))
            store.write_json(p, {"i": i}, indent=None)
            paths.append(p)
            assert store.read_json(p) == {"i": i}
        assert len(store._json_cache) <= 8
        assert os.path.abspath(store.SESSION_FILE) in store._json_pinned
    finally:
        store._JSON_CACHE_MAX = old_max
        for p in paths:
            store.drop_cache(p)
