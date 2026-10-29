# -*- coding: utf-8 -*-
"""通用观测层：写入 / 查询 / 聚合 / 清理，以及数学竞技场的采集钩子。"""
import json
import time

import pytest

from app import db, observe


@pytest.fixture
def tmpdb(tmp_path):
    """把数据库指向临时文件；用例结束后还原，避免污染其他测试。"""
    old = db.DB_FILE
    db.configure(tmp_path / "test.db")
    yield
    db.configure(old)


def test_record_and_query(tmpdb):
    assert observe.record(subject="数学分析", topic="极限",
                          value={"correct": False}, evidence="求 lim x->0")
    out = observe.query(days=14)
    assert out["count"] == 1
    row = out["items"][0]
    assert row["source"] == "chat"
    assert row["subject"] == "数学分析"
    assert json.loads(row["value"])["correct"] is False


def test_query_filters_by_subject_and_topic(tmpdb):
    observe.record(subject="数学分析", topic="极限", value={})
    observe.record(subject="高等代数", topic="可对角化", value={})
    assert observe.query(subject="高等代数")["count"] == 1
    assert observe.query(topic="极限")["count"] == 1
    assert observe.query(subject="不存在")["count"] == 0


def test_brief_aggregates_by_topic(tmpdb):
    for ok in (True, False, False):
        observe.record(subject="数学分析", topic="极限", value={"correct": ok})
    observe.record(subject="高等代数", topic="可对角化", value={"correct": True})
    text = observe.brief()
    assert text.startswith("【相关观测 · 近 14 天】")
    assert "数学分析 / 极限：3 次，对 1（33%）" in text
    assert "高等代数 / 可对角化：1 次，对 1（100%）" in text
    assert "样本少于 3 次" not in text          # 已评分 4 次，不再提示样本少


def test_brief_marks_small_sample(tmpdb):
    observe.record(subject="英语", topic="虚拟语气", value={"correct": False})
    text = observe.brief()
    assert "样本少于 3 次" in text


def test_brief_prefers_query_match(tmpdb):
    for _ in range(5):
        observe.record(subject="数学分析", topic="极限", value={"correct": True})
    observe.record(subject="英语", topic="虚拟语气", value={"correct": False})
    text = observe.brief("虚拟语气这里我总是错")
    assert text.index("英语 / 虚拟语气") < text.index("数学分析 / 极限")


def test_brief_empty_returns_empty(tmpdb):
    assert observe.brief() == ""


def test_record_truncates_long_fields(tmpdb):
    observe.record(subject="s" * 100, topic="t" * 100, evidence="e" * 2000)
    row = observe.recent()[0]
    assert len(row["subject"]) == observe.MAX_SUBJECT
    assert len(row["topic"]) == observe.MAX_TOPIC
    assert len(row["evidence"]) == observe.MAX_EVIDENCE


def test_prune_removes_old_rows(tmpdb):
    observe.record(subject="数学分析", topic="极限", value={})
    conn = db.get_db()
    conn.execute("UPDATE observations SET created_at = ?",
                 (time.time() - 200 * 86400,))
    conn.commit()
    assert observe.prune(180) is True
    assert observe.recent(days=365) == []


def test_record_many_writes_all(tmpdb):
    observe.record_many([
        {"subject": "数学分析", "topic": "极限", "value": {"correct": True}},
        {"subject": "数学分析", "topic": "连续", "value": {"correct": False}},
    ])
    assert observe.query()["count"] == 2


def test_math_answer_records_observation(tmpdb):
    from app import math_arena
    r = math_arena.new_round(level=1, ai_count=0, seed=7)
    q = r["questions"][0]
    math_arena.answer(r["id"], q["id"], q.get("answer"))
    rows = observe.recent()
    assert rows and rows[0]["source"] == "math_arena"
    data = json.loads(rows[0]["value"])
    assert data["correct"] is True


def test_observe_capabilities_registered():
    from app import capabilities, observe_caps  # noqa: F401
    assert capabilities.get("observe.record") is not None
    assert capabilities.get("observe.query") is not None


def test_summary_groups_by_subject_and_topic(tmpdb):
    for ok in (True, False, True):
        observe.record(subject="数学分析", topic="极限",
                       value={"correct": ok}, source="math_arena")
    observe.record(subject="数学分析", topic="重积分", evidence="计算二重积分")
    out = observe.summary(days=14)
    assert out["count"] == 4
    assert out["subjects"] == 1
    assert out["graded"] == 3
    assert out["correct"] == 2
    assert out["accuracy"] == 67
    assert out["last"] > 0
    groups = {g["topic"]: g for g in out["groups"]}
    assert groups["极限"]["attempts"] == 3
    assert groups["极限"]["accuracy"] == 67
    assert groups["极限"]["sources"] == {"math_arena": 3}
    assert groups["重积分"]["accuracy"] is None


def test_summary_filters_subject(tmpdb):
    observe.record(subject="数学分析", topic="极限")
    observe.record(subject="高等代数", topic="矩阵")
    out = observe.summary(subject="高等代数")
    assert out["count"] == 1
    assert out["groups"][0]["topic"] == "矩阵"


def test_summary_empty(tmpdb):
    out = observe.summary()
    assert out["count"] == 0
    assert out["groups"] == []
    assert out["accuracy"] is None
    assert out["last"] == 0


def test_observe_api_route_registered():
    from app.api import misc  # noqa: F401
    from app.registry import get
    assert get("GET", "/api/observe") is not None

