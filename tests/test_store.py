# -*- coding: utf-8 -*-
"""store：JSON 原子写 + mtime 缓存"""
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
