# -*- coding: utf-8 -*-
"""spill：落盘 / 取回 / locator 校验 / 按数量清理。"""
import os

import pytest

from app import spill


@pytest.fixture
def d(tmp_path, monkeypatch):
    monkeypatch.setattr(spill, "DATA_DIR", str(tmp_path))
    return str(tmp_path)


def test_roundtrip(d):
    body = "x" * 60000
    loc = spill.save("s1", "read_file", body, data_dir=d)
    assert spill.valid(loc)
    assert spill.load(loc, d) == body
    m = spill.meta(loc, d) or {}
    assert m.get("tool") == "read_file"
    assert m.get("chars") == 60000


def test_invalid_locator_rejected(d):
    # 防路径穿越：只接受 sp_ + 10 位 hex
    assert spill.load("../../etc/passwd", d) is None
    assert spill.load("sp_zzz", d) is None
    assert spill.meta("r1", d) is None


def test_gc_keeps_recent(d):
    locs = [spill.save("s", "t", "x" * 10, data_dir=d, keep=2, max_mb=0)
            for _ in range(4)]
    left = [n for n in os.listdir(os.path.join(d, "spills")) if n.endswith(".txt")]
    assert len(left) == 2
    assert spill.load(locs[-1], d) is not None
    assert spill.load(locs[0], d) is None


def test_disable_limits(d):
    # keep=0 且 max_mb=0 = 不限：全部保留
    for _ in range(5):
        spill.save("s", "t", "x", data_dir=d, keep=0, max_mb=0)
    left = [n for n in os.listdir(os.path.join(d, "spills")) if n.endswith(".txt")]
    assert len(left) == 5
