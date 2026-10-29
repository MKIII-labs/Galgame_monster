# -*- coding: utf-8 -*-
"""研究道场：前测 / 后测。"""
import copy

import pytest

from app.dojo import assess, packs

UNIT = {
    "id": "u1",
    "ability": "method",
    "primitive": "single_choice",
    "judge": "L2",
    "minutes": 2,
    "payload": {
        "situation": "这是一道用于测试的判断题。",
        "options": [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}, {"key": "C", "text": "丙"}],
    },
    "expert": {"choice": "B", "why": "理由", "signals": ["信号"], "traps": {"A": "坑", "C": "坑"}},
    "card": {"title": "卡", "trigger": "触发", "first_step": "第一步", "pitfall": "坑"},
    "source_ref": {"doc": "test"},
}


@pytest.fixture(autouse=True)
def _clean():
    assess.reset_store()
    packs.reset_cache()
    yield
    assess.reset_store()
    packs.reset_cache()


def _answers(phase):
    pack = packs.get_pack("cmc-calculus")
    ids = pack["manifest"]["assessment"][phase]
    by_id = {u["id"]: u for u in pack["units"]}
    return [(i, by_id[i]["expert"]["choice"]) for i in ids]


def test_start_hides_expert():
    out = assess.start("cmc-calculus", "pre")
    assert out["total"] == 8
    assert len(out["units"]) == 8
    assert all("expert" not in u for u in out["units"])
    assert out["phase"] == "pre"


def test_pre_and_post_cover_same_abilities():
    pre = assess.start("cmc-calculus", "pre")
    post = assess.start("cmc-calculus", "post")
    assert {u["ability"] for u in pre["units"]} == {u["ability"] for u in post["units"]}
    assert {u["id"] for u in pre["units"]}.isdisjoint({u["id"] for u in post["units"]})


def test_full_run_scores_all_correct():
    out = assess.start("cmc-calculus", "pre")
    for uid, choice in _answers("pre"):
        r = assess.answer(out["session_id"], uid, choice, elapsed_ms=1500)
        assert r["correct"] is True
        assert r["expert"]["why"]
    summary = assess.finish(out["session_id"])
    assert summary["total"] == 8
    assert summary["correct"] == 8
    assert summary["rate"] == 1.0
    assert summary["elapsed_ms"] == 12000
    assert len(summary["by_ability"]) >= 3


def test_wrong_then_repeat_rejected():
    out = assess.start("cmc-calculus", "pre")
    uid, choice = _answers("pre")[0]
    wrong = "A" if choice != "A" else "B"
    r = assess.answer(out["session_id"], uid, wrong)
    assert r["correct"] is False
    with pytest.raises(assess.AssessError):
        assess.answer(out["session_id"], uid, choice)


def test_finish_requires_all_answers():
    out = assess.start("cmc-calculus", "pre")
    with pytest.raises(assess.AssessError):
        assess.finish(out["session_id"])


def test_bad_phase():
    with pytest.raises(assess.AssessError):
        assess.start("cmc-calculus", "mid")


def test_pack_without_assessment(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "USER_PACKS_DIR", str(tmp_path))
    packs.reset_cache()
    packs.write_pack("plain", {
        "id": "plain",
        "name": "无测评",
        "version": 1,
        "grade": "personal",
        "alignment": {"standard": "测试", "dimensions": ["x"]},
        "ability_weights": {"method": 1},
        "entry": {"minutes": 2, "first_unit": "u1"},
        "sequence": ["u1"],
    }, [copy.deepcopy(UNIT)])
    with pytest.raises(assess.AssessError):
        assess.start("plain", "pre")
