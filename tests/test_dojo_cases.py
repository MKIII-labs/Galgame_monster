# -*- coding: utf-8 -*-
"""研究道场：完整建模案例。"""
import pytest

from app.dojo import cases


@pytest.fixture(autouse=True)
def _clean():
    cases.reset_cache()
    cases.reset_store()
    yield
    cases.reset_store()
    cases.reset_cache()


def _standard(step):
    expert = step["expert"]
    primitive = step["primitive"]
    if primitive in ("single_choice", "multi_select"):
        return expert["choice"]
    if primitive == "numeric":
        return expert["value"]
    if primitive == "order":
        return expert["order"]
    if primitive == "pair":
        return expert["pairs"]
    return None


def test_cases_load_and_shape():
    loaded = cases.load_cases()
    assert len(loaded) >= 2
    for case in loaded:
        assert len(case["steps"]) == 6
        prims = {s["primitive"] for s in case["steps"]}
        abilities = {s["ability"] for s in case["steps"]}
        assert len(prims) >= 3
        assert len(abilities) >= 4
        assert case["source"].get("url")


def test_public_case_hides_expert_and_payload():
    out = cases.start("cumcm-2023b")
    assert out["total"] == 6
    assert "expert" not in out["step"]
    assert "card" not in out["step"]
    assert out["step"]["payload"].get("situation")
    for step in out["case"]["steps"]:
        assert "expert" not in step and "payload" not in step


@pytest.mark.parametrize("case_id", ["cumcm-2023b", "cumcm-2024c"])
def test_standard_answers_finish_clean(case_id):
    case = cases.get_case(case_id)
    out = cases.start(case_id)
    for step in case["steps"]:
        r = cases.answer(out["session_id"], step["id"], _standard(step), elapsed_ms=1000)
        assert r["correct"] is True
        assert r["score"] == 1.0
    report = cases.finish(out["session_id"])
    assert report["total"] == 6
    assert report["avg_score"] == 1.0
    assert report["first_break"] is None
    assert len(report["by_ability"]) >= 4
    assert report["elapsed_ms"] == 6000


def test_wrong_answer_is_reported_as_break():
    case = cases.get_case("cumcm-2023b")
    out = cases.start("cumcm-2023b")
    first = case["steps"][0]
    wrong = "B" if _standard(first) != "B" else "A"
    r = cases.answer(out["session_id"], first["id"], wrong)
    assert r["correct"] is False
    for step in case["steps"][1:]:
        cases.answer(out["session_id"], step["id"], _standard(step))
    report = cases.finish(out["session_id"])
    assert report["first_break"]["index"] == 0
    assert report["first_break"]["step_id"] == first["id"]
    assert "断在第 1 步" in report["verdict"]


def test_must_answer_in_order():
    case = cases.get_case("cumcm-2023b")
    out = cases.start("cumcm-2023b")
    second = case["steps"][1]
    with pytest.raises(cases.CaseError):
        cases.answer(out["session_id"], second["id"], _standard(second))


def test_duplicate_submit_rejected():
    case = cases.get_case("cumcm-2023b")
    out = cases.start("cumcm-2023b")
    first = case["steps"][0]
    cases.answer(out["session_id"], first["id"], _standard(first))
    with pytest.raises(cases.CaseError):
        cases.answer(out["session_id"], first["id"], _standard(first))


def test_finish_requires_all_steps():
    out = cases.start("cumcm-2024c")
    with pytest.raises(cases.CaseError):
        cases.finish(out["session_id"])


def test_unknown_case():
    with pytest.raises(cases.CaseError):
        cases.start("not-a-case")
