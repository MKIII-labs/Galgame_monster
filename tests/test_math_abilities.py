# -*- coding: utf-8 -*-
"""竞技场：知识点 → 8 维能力映射与聚合。"""
from app import math_arena as ma
from app.math import abilities


def test_all_concept_skills_mapped():
    skills = {q["skill"] for q in ma.CONCEPTS if q.get("skill")}
    assert set(abilities.SKILL_ABILITY) == skills
    assert len(skills) == 96
    assert all(v in abilities.ABILITIES for v in abilities.SKILL_ABILITY.values())


def test_ability_meta_shape():
    meta = ma.ability_meta()
    assert len(meta["abilities"]) == 8
    assert len(meta["skills"]) == 96
    assert {a["id"] for a in meta["abilities"]} == set(abilities.ABILITIES)


def test_aggregate_by_skill_and_ability():
    rows = [
        {"skill": "数列极限", "correct": True, "score": 100},
        {"skill": "级数", "correct": False, "score": 0},
        {"skill": "级数", "correct": True, "score": 100},
    ]
    out = abilities.aggregate(rows)
    by_skill = {s["skill"]: s for s in out["by_skill"]}
    assert by_skill["级数"]["attempts"] == 2
    assert by_skill["级数"]["correct"] == 1
    assert by_skill["级数"]["accuracy"] == 50.0
    by_ability = {a["ability"]: a for a in out["by_ability"]}
    assert by_ability["method"]["attempts"] == 1      # 数列极限
    assert by_ability["condition"]["attempts"] == 2   # 级数两题
    assert set(by_ability) == {"method", "condition"}


def test_round_finish_exposes_ability():
    r = ma.new_round(level=3, ai_count=0, seed=7)
    for q in r["questions"]:
        value = q["answer"]
        if q["kind"] == "tf":
            value = "true" if value else "false"
        ma.answer(r["id"], q["id"], value, elapsed_ms=1000)
    out = ma.finish(r["id"])
    assert out["correct"] == len(r["questions"])
    assert out["by_ability"]
    assert {a["ability"] for a in out["by_ability"]} <= set(abilities.ABILITIES)


def test_paper_grade_exposes_ability():
    p = ma.new_paper(course="analysis1", count=5, seed=3)
    paper = ma._PAPERS[p["paper_id"]]
    answers = {q["id"]: q["answer"] for q in paper["questions"]}
    out = ma.grade_paper(p["paper_id"], answers, time_ms=5000)
    assert out["by_ability"]
    assert {a["ability"] for a in out["by_ability"]} <= set(abilities.ABILITIES)
