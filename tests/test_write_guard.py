# -*- coding: utf-8 -*-
"""并行写隔离：路径登记、同批冲突与 strict 拒绝。"""
from app import write_guard


def test_paths_of_write_tools():
    assert write_guard.paths_of("write_file", {"path": "app/a.py"}) == ["app/a.py"]
    assert write_guard.paths_of("edit_file", {"path": "./app/b.py"}) == ["app/b.py"]
    assert write_guard.paths_of("move_file",
                                {"from": "a.py", "to": "b.py"}) == ["a.py", "b.py"]
    # 目录类不参与冲突
    assert write_guard.paths_of("make_dir", {"path": "newdir"}) == []


def test_serial_claim_reports_previous_owner():
    batch = "b_serial"
    ok, prev, _ = write_guard.claim(batch, "app/a.py", "reviewer", "serial")
    assert ok and prev == ""
    ok, prev, _ = write_guard.claim(batch, "app/a.py", "implementer", "serial")
    assert ok and prev == "reviewer"
    assert write_guard.conflicts(batch) == [("app/a.py", ["implementer", "reviewer"])]
    write_guard.forget(batch)


def test_strict_rejects_second_writer():
    batch = "b_strict"
    assert write_guard.claim(batch, "app/a.py", "a1", "strict")[0] is True
    ok, prev, _ = write_guard.claim(batch, "app/a.py", "a2", "strict")
    assert ok is False and prev == "a1"
    # 同一个代理再写不算冲突
    assert write_guard.claim(batch, "app/a.py", "a1", "strict")[0] is True
    write_guard.forget(batch)


def test_off_mode_skips_registry():
    assert write_guard.claim("b_off", "app/a.py", "a1", "off")[0] is True
    assert write_guard.conflicts("b_off") == []


def test_same_owner_not_a_conflict():
    batch = "b_same"
    write_guard.claim(batch, "app/a.py", "only", "serial")
    write_guard.claim(batch, "app/a.py", "only", "serial")
    assert write_guard.conflicts(batch) == []
    write_guard.forget(batch)
