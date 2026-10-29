# -*- coding: utf-8 -*-
"""研究记录：完成报告落盘、列表、详情、删除。"""
import pytest

from app.dojo import records


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "RECORDS_FILE", str(tmp_path / "research_records.json"))
    records.reset_store()
    yield
    records.reset_store()


def _report(inquiry_id="t-inquiry", title="测试研究"):
    return {
        "inquiry_id": inquiry_id,
        "title": title,
        "subject": "数学分析",
        "track": "本科",
        "avg_score": 0.9,
        "verdict": "研究链完整",
        "acts": [{"index": 0, "label": "先猜", "score": 1.0}],
        "by_ability": [],
        "artifact": {
            "coverage": 0.8,
            "text": "我的结论",
            "hits": [],
            "missing": [],
            "expert_sample": "专家示范",
            "prompt": "研究问题",
        },
    }


def test_save_list_get():
    saved = records.save(_report())
    assert saved["id"].startswith("rr_")
    rows = records.list_records()
    assert len(rows) == 1
    assert rows[0]["title"] == "测试研究"
    assert rows[0]["coverage"] == 0.8
    got = records.get_record(saved["id"])
    assert got["artifact"]["text"] == "我的结论"
    assert got["artifact"]["prompt"] == "研究问题"


def test_delete():
    saved = records.save(_report())
    records.delete_record(saved["id"])
    assert records.list_records() == []
    with pytest.raises(ValueError):
        records.get_record(saved["id"])


def test_newest_first():
    first = records.save(_report("a", "第一"))
    second = records.save(_report("b", "第二"))
    rows = records.list_records()
    assert [r["id"] for r in rows][:2] == [second["id"], first["id"]]


def test_missing_record():
    with pytest.raises(ValueError):
        records.get_record("nope")
    with pytest.raises(ValueError):
        records.delete_record("nope")
