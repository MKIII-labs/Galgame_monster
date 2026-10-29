# -*- coding: utf-8 -*-
"""历史压缩分隔线：同一压缩事件不能留下两条 at 相同的标记。"""
from app import context
from app import llm
from app.sessions import compress_marks_for, trim_compress_marks


def test_compress_marks_for_dedupes_same_at():
    marks = [
        {"at": 123, "index": 32, "upto": 32, "count": 32},
        {"at": 123, "index": 40, "upto": 32, "count": 32},
    ]
    out = compress_marks_for({"compress_marks": marks})
    assert len(out) == 1
    assert out[0]["index"] == 40          # 保留真正的压缩位置，而不是补的旧标记


def test_compress_history_does_not_backfill_duplicate(monkeypatch):
    s = {"id": "s1", "summary_upto": 0,
         "messages": [{"role": "user", "content": "hello"},
                      {"role": "assistant", "content": "world"}]}
    monkeypatch.setattr(context, "get_api_conf", lambda *a, **kw: ("k", "u", "m"))
    monkeypatch.setattr(context, "context_limits", lambda: (1000, 0))
    monkeypatch.setattr(context, "keep_from_by_tokens", lambda *a, **kw: 1)
    monkeypatch.setattr(context, "_split_summary", lambda raw: ("摘要", []))
    monkeypatch.setattr(llm, "_quick_chat", lambda *a, **kw: "摘要")

    assert context.compress_history(s, "") is True
    assert len(s["compress_marks"]) == 1
    assert s["compress_marks"][0]["index"] == 2


def test_compress_marks_dedupe_by_upto_even_if_at_differs():
    """同一个压缩点（upto）只留一条，即便 at 因时钟漂移不同"""
    marks = [
        {"at": 100, "index": 32, "upto": 32, "count": 32},
        {"at": 205, "index": 40, "upto": 32, "count": 32},
    ]
    out = compress_marks_for({"compress_marks": marks})
    assert len(out) == 1
    assert out[0]["index"] == 40      # 保留消息流里更靠后的真实位置


def test_trim_compress_marks_dedupes_after_truncate():
    """编辑 / 撤回后重复写入的同一压缩点，不能留两条分隔线"""
    s = {"messages": [{"role": "user", "content": "x"} for _ in range(10)],
         "compress_marks": [
             {"at": 1, "index": 8, "upto": 8, "count": 8},
             {"at": 2, "index": 8, "upto": 8, "count": 8},
         ]}
    trim_compress_marks(s, drop_after=8)
    assert len(s["compress_marks"]) == 1


def test_trim_compress_marks_drops_marks_after_edit_point():
    s = {"messages": [{"role": "user", "content": "x"} for _ in range(10)],
         "compress_marks": [
             {"at": 1, "index": 3, "upto": 3, "count": 3},
             {"at": 2, "index": 8, "upto": 8, "count": 8},
         ]}
    trim_compress_marks(s, drop_after=5)
    assert [m["upto"] for m in s["compress_marks"]] == [3]
