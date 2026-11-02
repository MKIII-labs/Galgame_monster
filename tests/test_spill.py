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


def test_find_offsets_returns_line_and_absolute_offset():
    text = "alpha\nbeta ERROR here\ngamma\nERROR again\n"
    total, hits = spill.find_offsets(text, "ERROR", context=1)
    assert total == 2 and len(hits) == 2
    assert hits[0]["line"] == 2
    assert hits[0]["offset"] == text.index("ERROR")
    assert hits[0]["before"] == ["alpha"]
    assert hits[0]["after"] == ["gamma"]
    # 偏移是绝对位置：拿它去切片能直接读到命中处
    assert text[hits[1]["offset"]:].startswith("ERROR")


def test_find_offsets_case_and_regex_and_cap():
    text = "Warn: a\nwarn: b\nfatal: c\n"
    assert spill.find_offsets(text, "warn", ignore_case=True)[0] == 2
    assert spill.find_offsets(text, "warn")[0] == 1
    assert spill.find_offsets(text, r"fa[a-z]+", regex=True)[0] == 1
    total, hits = spill.find_offsets(text * 10, "warn", max_hits=2)
    assert total == 10 and len(hits) == 2


def test_find_offsets_rejects_bad_regex():
    with pytest.raises(ValueError):
        spill.find_offsets("abc", "(", regex=True)
