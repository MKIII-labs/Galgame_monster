# -*- coding: utf-8 -*-
"""研究道场：深度研究任务。"""
import pytest

from app.dojo import research


@pytest.fixture(autouse=True)
def _clean():
    research.reset_cache()
    research.reset_store()
    yield
    research.reset_store()
    research.reset_cache()


def _standard(act):
    expert = act["expert"]
    primitive = act["primitive"]
    if primitive in ("single_choice", "multi_select"):
        return expert["choice"]
    if primitive == "numeric":
        return expert["value"]
    if primitive == "order":
        return expert["order"]
    if primitive == "pair":
        return expert["pairs"]
    return None


def _walk(inquiry_id, answer_all=True):
    q = research.get_inquiry(inquiry_id)
    out = research.start(inquiry_id)
    if answer_all:
        for act in q["acts"]:
            research.answer(out["session_id"], act["id"], _standard(act))
    return q, out["session_id"]


def test_collections_load():
    cols = research.load_collections()
    assert cols
    assert sum(len(c["items"]) for c in cols) >= 24
    for col in cols:
        for q in col["items"]:
            assert len(q["acts"]) == 6
            assert len({a["primitive"] for a in q["acts"]}) >= 3
            assert len({a["ability"] for a in q["acts"]}) >= 4
            assert len(q["artifact"]["rubric"]) >= 3


def test_public_hides_answers():
    out = research.start("ma-uniform-convergence")
    assert "expert" not in out["act"]
    assert "card" not in out["act"]
    assert out["act"]["payload"].get("situation")
    assert out["inquiry"]["artifact"].get("prompt")
    # rubric 关键词与专家示范不能在提交前下发
    blob = str(out["inquiry"])
    assert "expert_sample" not in blob
    assert "keywords" not in blob


@pytest.mark.parametrize("inquiry_id", [
    "ma-uniform-convergence", "ha-determinant", "geo-conic-classification",
    "nt-congruence", "aa-homomorphism-kernel", "ode-existence-lipschitz",
    "dm-euler-circuit", "mm-model-building",
    "ma-completeness", "ma-mvt", "ha-basis-dimension", "ha-diagonalization",
    "nt-crt", "nt-euler-theorem", "aa-lagrange", "aa-group-action",
    "ode-linear-structure", "ode-stability", "dm-recurrence", "dm-pigeonhole",
    "geo-plane-pencil", "geo-quadric", "mm-evaluation", "mm-optimization",
])
def test_standard_run_and_artifact(inquiry_id):
    q, sid = _walk(inquiry_id)
    art = research.submit_artifact(sid, q["artifact"]["expert_sample"])
    assert art["scored"] is False
    assert art["coverage"] >= 0.8
    report = research.finish(sid)
    assert report["total"] == 6
    assert report["avg_score"] == 1.0
    assert report["first_break"] is None
    assert report["artifact"]["coverage"] >= 0.8
    assert report["verdict"] == "研究链完整"


def test_wrong_act_becomes_break():
    q = research.get_inquiry("ha-determinant")
    out = research.start(q["id"])
    first = q["acts"][0]
    wrong = "B" if _standard(first) != "B" else "A"
    research.answer(out["session_id"], first["id"], wrong)
    for act in q["acts"][1:]:
        research.answer(out["session_id"], act["id"], _standard(act))
    research.submit_artifact(out["session_id"], q["artifact"]["expert_sample"])
    report = research.finish(out["session_id"])
    assert report["first_break"]["index"] == 0
    assert "第 1 幕" in report["verdict"]


def test_must_answer_in_order():
    q = research.get_inquiry("geo-conic-classification")
    out = research.start(q["id"])
    with pytest.raises(research.ResearchError):
        research.answer(out["session_id"], q["acts"][1]["id"], _standard(q["acts"][1]))


def test_artifact_requires_all_acts():
    _q, sid = _walk("ma-uniform-convergence", answer_all=False)
    with pytest.raises(research.ResearchError):
        research.submit_artifact(sid, "x" * 80)
    with pytest.raises(research.ResearchError):
        research.finish(sid)


def test_artifact_min_length():
    q, sid = _walk("ma-uniform-convergence")
    with pytest.raises(research.ResearchError):
        research.submit_artifact(sid, "太短")
    assert q["artifact"]["prompt"]


def test_revise_only_once():
    q, sid = _walk("ma-uniform-convergence")
    research.submit_artifact(sid, q["artifact"]["expert_sample"])
    out = research.revise_artifact(sid, q["artifact"]["expert_sample"])
    assert out["revisions_left"] == 0
    with pytest.raises(research.ResearchError):
        research.revise_artifact(sid, q["artifact"]["expert_sample"])


def test_state_resume():
    q, sid = _walk("ma-uniform-convergence", answer_all=False)
    first = q["acts"][0]
    research.answer(sid, first["id"], _standard(first))
    st = research.state(sid)
    assert st["index"] == 1
    assert st["stage"] == "acts"
    assert st["act"]["id"] == q["acts"][1]["id"]
    assert st["answered"][0]["act_id"] == first["id"]


def test_finished_blocks_answers():
    q, sid = _walk("ma-uniform-convergence")
    research.submit_artifact(sid, q["artifact"]["expert_sample"])
    research.finish(sid)
    assert research.state(sid)["finished"] is True
    with pytest.raises(research.ResearchError):
        research.answer(sid, q["acts"][0]["id"], _standard(q["acts"][0]))


def test_paper_index_covers_ten_years():
    out = research.list_paper_index()
    by_id = {c["id"]: c for c in out}
    assert {"CMC", "CUMCM"} <= set(by_id)
    cmc = by_id["CMC"]
    assert cmc["total"] >= 100
    assert cmc["years"] == list(range(2025, 2015, -1))
    # 同一年 A/B 卷不能被合并成一个卷
    papers_2024 = [p for p in cmc["papers"] if p["year"] == 2024]
    assert {p["paper"] for p in papers_2024} == {"数学A类", "数学B类"}
    assert all(len(p["problems"]) == 6 for p in papers_2024)


def test_paper_index_marks_adapted_cases():
    cumcm = {c["id"]: c for c in research.list_paper_index()}["CUMCM"]
    assert cumcm["total"] == 30
    assert cumcm["adapted"] >= 2
    hit = [q for p in cumcm["papers"] if p["year"] == 2023
           for q in p["problems"] if q["no"] == "B"]
    assert hit and hit[0]["item"] == "cumcm-2023b"
    assert hit[0]["adapted"] is True


def test_paper_index_api_handler_exists():
    from app.api import dojo as dojo_api
    assert callable(dojo_api._api_dojo_paper_index)
