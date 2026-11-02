# -*- coding: utf-8 -*-
"""会话列表分组：沙箱路径归一化 + slim_sessions 回传分组字段。"""
from app import sessions


def test_sandbox_group_empty():
    assert sessions.sandbox_group("") == ("", "")
    assert sessions.sandbox_group(None) == ("", "")


def test_sandbox_group_normalizes_variants():
    a = sessions.sandbox_group(r"D:\Tools\Proj")
    b = sessions.sandbox_group("d:/tools/proj/")
    assert a[0] == b[0]                # 分组 key：大小写 / 斜杠 / 尾斜杠都收敛
    assert a[0] == a[0].lower()
    # 显示名保留用户写的原样（组名取该组第一条会话的写法）
    assert {a[1], b[1]} == {"Proj", "proj"}


def test_slim_sessions_adds_group_fields():
    data = {
        "current": "s1",
        "sessions": [
            {"id": "s1", "title": "A", "updated_at": 2, "sandbox": r"D:\Work\Alpha"},
            {"id": "s2", "title": "B", "updated_at": 1, "sandbox": ""},
        ],
    }
    out = sessions.slim_sessions(data)
    rows = {s["id"]: s for s in out["sessions"]}
    assert rows["s1"]["sandbox_key"] == sessions.sandbox_group(r"D:\Work\Alpha")[0]
    assert rows["s1"]["sandbox_name"] == "Alpha"
    assert rows["s2"]["sandbox_key"] == ""
    assert rows["s2"]["sandbox_name"] == ""
    # 原有字段不能被挤掉
    assert rows["s1"]["messages"] is not None
    assert rows["s2"]["messages"] is None


def test_reorder_keeps_unlisted_in_place():
    data = {"sessions": [{"id": "a"}, {"id": "b"}, {"id": "x"}, {"id": "c"}]}
    assert sessions.reorder_sessions(data, ["c", "a"]) == ""
    # 只置换 a / c 占用的槽位，b 与未列出的 x 不动
    assert [s["id"] for s in data["sessions"]] == ["c", "b", "x", "a"]


def test_reorder_rejects_bad_ids():
    data = {"sessions": [{"id": "a"}, {"id": "b"}]}
    assert sessions.reorder_sessions(data, ["nope"]) != ""
    assert sessions.reorder_sessions(data, ["a", "a"]) != ""
    assert sessions.reorder_sessions(data, []) != ""
    assert [s["id"] for s in data["sessions"]] == ["a", "b"]
