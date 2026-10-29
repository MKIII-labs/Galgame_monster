# -*- coding: utf-8 -*-
"""数学竞技场后端：题库结构 / 答案正确性 / 判分 / 对局生命周期。"""
import math
import random

import pytest

from app import math_arena as ma


@pytest.fixture(autouse=True)
def _clean():
    ma.reset_store()
    yield
    ma.reset_store()


# ---------------- 纯函数与题库 ----------------

def test_number_theory_helpers():
    assert ma._gcd(84, 132) == 12
    assert ma._phi(36) == 12
    assert ma._phi(97) == 96                     # 素数
    assert ma._divisor_count(36) == 9
    assert ma._divisor_count(97) == 2


def test_rank_helper():
    assert ma._rank([[1, 2], [2, 4]]) == 1
    assert ma._rank([[1, 0], [0, 1]]) == 2
    assert ma._rank([[0, 0], [0, 0]]) == 0
    assert ma._rank([[1, 2, 3], [2, 4, 6], [0, 1, 1]]) == 2


def test_concept_bank_is_wellformed():
    prompts = set()
    for q in ma.CONCEPTS:
        assert q["subject"] in ma.SUBJECT_NAMES
        assert q["kind"] in ("tf", "counter")
        assert q["prompt"] and q["explain"] and q["skill"]
        assert q["prompt"] not in prompts, "重复题目：%s" % q["prompt"]
        prompts.add(q["prompt"])
        if q["kind"] == "tf":
            assert isinstance(q["answer"], bool)
        else:
            ids = [o["id"] for o in q["options"]]
            assert len(ids) >= 3 and q["answer"] in ids
    for subject in ma.SUBJECT_NAMES:
        assert sum(1 for q in ma.CONCEPTS if q["subject"] == subject) >= 6


def test_round_has_expected_mix_and_hides_answers():
    for level in (1, 3, 5):
        for seed in range(8):
            r = ma.new_round(level=level, seed=seed)
            assert len(r["questions"]) == ma.QUESTION_COUNT
            kinds = [q["kind"] for q in r["questions"]]
            assert kinds.count("calc") == 3
            assert kinds.count("tf") == 1
            assert kinds.count("counter") == 1
            pub = ma.public_round(r)
            assert "answer" not in pub
            for q in pub["questions"]:
                assert "answer" not in q and "explain" not in q
                assert q["subject_name"] in ma.SUBJECT_NAMES.values()
                if q["kind"] == "counter":
                    assert len(q["options"]) >= 3
                if q["kind"] == "tf":
                    assert [o["id"] for o in q["options"]] == ["true", "false"]
            assert len(pub["players"]) == 4          # 你 + 3 个 AI


def test_calc_answers_are_finite():
    for level in (1, 3, 5):
        for seed in range(40):
            r = ma.new_round(level=level, seed=seed)
            for q in r["questions"]:
                if q["kind"] == "calc":
                    assert isinstance(q["answer"], (int, float))
                    assert math.isfinite(float(q["answer"]))
                    assert q["explain"]


# ---------------- 答题与判分 ----------------

def test_answer_correct_and_finish():
    r = ma.new_round(level=3, seed=1)
    for q in r["questions"]:
        value = q["answer"]
        if q["kind"] == "tf":
            value = "true" if value else "false"
        out = ma.answer(r["id"], q["id"], value, elapsed_ms=100)
        assert out["correct"] is True
        assert out["score"] >= 100
        assert len(out["ai"]) == 3
        for a in out["ai"]:
            assert a["reason"]
            assert isinstance(a["score"], int)
            assert a["player_id"].startswith("ai_")
    fin = ma.finish(r["id"])
    assert fin["correct"] == 5 and fin["score"] > 0
    assert fin["rank"] == 1


def test_wrong_answer_and_partial_credit():
    r = ma.new_round(level=2, seed=2)
    q = next(x for x in r["questions"] if x["kind"] == "calc")
    out = ma.answer(r["id"], q["id"], "not-a-number", elapsed_ms=1000)
    assert out["correct"] is False and out["score"] == 0
    # 同题重复提交应被拒绝
    with pytest.raises(ma.MathError):
        ma.answer(r["id"], q["id"], q["answer"], elapsed_ms=1000)


def _find_calc(exact_int):
    """找一个含指定类型计算题的对局（整数答案 / 小数答案）。"""
    for seed in range(80):
        r = ma.new_round(level=3, seed=seed)
        for q in r["questions"]:
            if q["kind"] != "calc":
                continue
            if ma._is_exact_int(q["answer"]) == exact_int:
                return r, q
    return None, None


def test_integer_calc_requires_exact_answer():
    r, q = _find_calc(True)
    assert q is not None
    out = ma.answer(r["id"], q["id"], float(q["answer"]) + 0.4, 1000)
    assert out["correct"] is False
    assert out["score"] == 0          # 整数题没有部分分


def test_decimal_calc_keeps_partial_credit():
    r, q = _find_calc(False)
    assert q is not None
    near = float(q["answer"]) * 1.05   # 偏 5%，落在 1% 容差外
    out = ma.answer(r["id"], q["id"], near, 1000)
    assert out["correct"] is False
    assert 0 < out["score"] < 100


def test_ai_integer_answers_are_exact():
    """整数题 AI 说「算对了」就必须给出精确整数，不能拿 719.38 冒充 720。"""
    checked = 0
    for seed in range(40):
        r = ma.new_round(level=3, seed=seed)
        q = next((x for x in r["questions"]
                  if x["kind"] == "calc" and ma._is_exact_int(x["answer"])), None)
        if q is None:
            continue
        out = ma.answer(r["id"], q["id"], q["answer"], 500)
        for a in out["ai"]:
            if a["correct"]:
                checked += 1
                assert float(a["value"]) == float(q["answer"])
        ma.reset_store()
    assert checked > 0, "40 个种子里至少应出现一次 AI 答对整数题"


def test_ai_wrong_reason_does_not_leak_explanation():
    """AI 选错时不能把正确答案的解析写在错误选项后面（自相矛盾且泄露思路）。"""
    wrong = []
    q = None
    for seed in range(20):
        r = ma.new_round(level=2, seed=seed)
        q = next((x for x in r["questions"] if x["kind"] in ("tf", "counter")), None)
        if q is None:
            continue
        value = q["answer"] if q["kind"] == "counter" else (
            "true" if q["answer"] else "false")
        out = ma.answer(r["id"], q["id"], value, 500)
        wrong = [a for a in out["ai"] if not a["correct"]]
        if wrong:
            break
        ma.reset_store()
    assert wrong, "20 个种子里应出现至少一个 AI 判断错误"
    assert q["explain"]
    for a in wrong:
        assert q["explain"] not in a["reason"]


def test_finish_requires_all_answers():
    r = ma.new_round(level=1, seed=3)
    with pytest.raises(ma.MathError):
        ma.finish(r["id"])


def test_unknown_round_and_question():
    with pytest.raises(ma.MathError):
        ma.answer("nope", "q1", 1, 0)
    r = ma.new_round(level=1, seed=4)
    with pytest.raises(ma.MathError):
        ma.answer(r["id"], "q9", 1, 0)


def test_level_is_clamped():
    assert ma.new_round(level=99, seed=5)["level"] == 5
    assert ma.new_round(level=-3, seed=5)["level"] == 1
    assert ma.new_round(level="x", seed=5)["level"] == 3


def test_ai_accuracy_responds_to_level():
    """高段位 AI 应明显比低段位更准（用固定种子统计，避免随机波动）。"""
    def accuracy(level):
        hits = total = 0
        for seed in range(30):
            r = ma.new_round(level=level, seed=seed)
            for q in r["questions"]:
                value = q["answer"]
                if q["kind"] == "tf":
                    value = "true" if value else "false"
                out = ma.answer(r["id"], q["id"], value, 5000)
                for a in out["ai"]:
                    total += 1
                    hits += 1 if a["correct"] else 0
        return hits / total
    assert accuracy(5) > accuracy(1) + 0.05


def test_public_round_never_leaks_answers():
    r = ma.new_round(level=3, seed=7)
    pub = ma.public_round(r)
    for q in pub["questions"]:
        assert "answer" not in q
        assert "explain" not in q
        assert "ai_hint" not in q


# ---------------- 新增课程与学习机制 ----------------

def test_new_subject_bank_coverage():
    subjects = list(ma.SUBJECT_NAMES)
    assert len(subjects) == 12
    for subject in subjects:
        items = [q for q in ma.CONCEPTS if q["subject"] == subject]
        assert len(items) >= 6, subject
        assert any(q["kind"] == "tf" for q in items), subject
        assert any(q["kind"] == "counter" for q in items), subject
        assert subject in ma.CALC_GENERATORS, subject


def test_course_bank_coverage():
    assert len(ma.COURSE_ORDER) == 15
    for course in ma.COURSE_ORDER:
        items = [q for q in ma.CONCEPTS if ma._concept_course(q) == course]
        assert len(items) >= 6, course
        assert any(q["kind"] == "tf" for q in items), course
        assert any(q["kind"] == "counter" for q in items), course
        assert course in ma.COURSE_GENERATORS, course


def test_single_course_rounds_use_only_that_course():
    for course in ma.COURSE_ORDER:
        for seed in range(5):
            r = ma.new_round(level=3, seed=seed, courses=[course])
            assert all(q["course"] == course for q in r["questions"]), (course, seed)
            kinds = [q["kind"] for q in r["questions"]]
            assert kinds.count("calc") == 3
            assert kinds.count("tf") == 1
            assert kinds.count("counter") == 1
            pub = ma.public_round(r)
            assert [c["id"] for c in pub["courses"]] == [course]


def test_parent_subject_expands_to_terms():
    r = ma.new_round(level=3, seed=2, subjects=["analysis"])
    assert {q["course"] for q in r["questions"]} <= {
        "analysis1", "analysis2", "analysis3"}
    assert {q["subject"] for q in r["questions"]} == {"analysis"}
    pub = ma.public_round(r)
    assert [s["id"] for s in pub["subjects"]] == ["analysis"]
    assert {c["id"] for c in pub["courses"]} <= {
        "analysis1", "analysis2", "analysis3"}

    r2 = ma.new_round(level=3, seed=2, subjects=["algebra"])
    assert {q["course"] for q in r2["questions"]} <= {"algebra1", "algebra2"}
    assert {q["subject"] for q in r2["questions"]} == {"algebra"}


def test_course_mapping_for_existing_skills():
    assert ma._course_of("analysis", "数列极限") == "analysis1"
    assert ma._course_of("analysis", "级数") == "analysis2"
    assert ma._course_of("analysis", "重积分") == "analysis3"
    assert ma._course_of("algebra", "行列式") == "algebra1"
    assert ma._course_of("algebra", "多项式") == "algebra1"
    assert ma._course_of("algebra", "二次型") == "algebra2"



def test_single_subject_rounds_use_only_that_subject():
    for subject in ma.SUBJECT_NAMES:
        for seed in range(6):
            r = ma.new_round(level=3, seed=seed, subjects=[subject])
            assert all(q["subject"] == subject for q in r["questions"]), (subject, seed)
            kinds = [q["kind"] for q in r["questions"]]
            assert kinds.count("calc") == 3
            assert kinds.count("tf") == 1
            assert kinds.count("counter") == 1
            pub = ma.public_round(r)
            assert [s["id"] for s in pub["subjects"]] == [subject]


def test_subject_subset_rounds_are_filtered():
    picked = ["probability", "ode", "modeling"]
    for seed in range(10):
        r = ma.new_round(level=4, seed=seed, subjects=picked)
        assert all(q["subject"] in picked for q in r["questions"])
        assert ma.public_round(r)["count"] == 5


def test_invalid_subjects_fall_back_to_all():
    r = ma.new_round(level=2, seed=1, subjects=["no-such-course"])
    assert set(q["subject"] for q in r["questions"]).issubset(set(ma.SUBJECT_NAMES))
    r2 = ma.new_round(level=2, seed=1, subjects=[])
    assert len(r2["questions"]) == 5


def test_hints_and_solution_steps_are_available():
    r = ma.new_round(level=3, seed=11, subjects=["probability", "ode", "complex"])
    for q in r["questions"]:
        pub_q = next(x for x in ma.public_round(r)["questions"] if x["id"] == q["id"])
        assert 1 <= len(pub_q["hints"]) <= 2
        assert "answer" not in pub_q and "explain" not in pub_q
        value = q["answer"]
        if q["kind"] == "tf":
            value = "true" if value else "false"
        out = ma.answer(r["id"], q["id"], value, 100)
        assert out["correct"] is True
        assert len(out["steps"]) >= 1
        assert out["exam_tip"]


def test_lp_helper_matches_bruteforce():
    rng = random.Random(2026)
    for _ in range(80):
        a_cap = rng.randint(1, 12)
        b_cap = rng.randint(1, 12)
        s_cap = rng.randint(max(a_cap, b_cap), a_cap + b_cap)
        p = rng.randint(1, 10)
        q = rng.randint(1, 10)
        point, val = ma._lp_max(a_cap, b_cap, s_cap, p, q)
        brute = max(p * x + q * y
                    for x in range(a_cap + 1) for y in range(b_cap + 1)
                    if x + y <= s_cap)
        assert val == brute, (a_cap, b_cap, s_cap, p, q, point, val, brute)


def test_new_generators_return_finite_answers_with_metadata():
    for subject in ["probability", "statistics", "ode", "complex",
                    "geometry", "numerical", "modeling"]:
        for seed in range(30):
            r = ma.new_round(level=3, seed=seed, subjects=[subject])
            for q in r["questions"]:
                if q["kind"] != "calc":
                    continue
                assert math.isfinite(float(q["answer"]))
                assert q["explain"] and q["steps"] and q["exam_tip"]
                assert 1 <= len(q["hints"]) <= 2


def test_arg_degrees_table():
    assert ma._arg_degrees(1, 0) == 0
    assert ma._arg_degrees(0, 1) == 90
    assert ma._arg_degrees(-1, 0) == 180
    assert ma._arg_degrees(1, 1) == 45
    assert ma._arg_degrees(-1, -1) == -135


def test_old_skills_get_specific_hints_and_scene():
    q = ma._decorate({
        "kind": "calc", "subject": "number", "skill": "最大公因数",
        "explain": "用辗转相除。", "hints": [],
    })
    assert "辗转" in q["hints"][0]
    assert q["scene"]


# ---------------- 期末模拟卷 ----------------

def test_paper_mix_table():
    assert ma._paper_mix(10) == (7, 2, 1)
    assert ma._paper_mix(15) == (10, 3, 2)
    assert ma._paper_mix(20) == (13, 4, 3)
    assert sum(ma._paper_mix(15)) == 15


def test_new_paper_is_single_course_and_hides_answers():
    for course in ("analysis1", "algebra2", "probability", "modeling"):
        p = ma.new_paper(course, count=10, seed=123)
        assert p["count"] >= 5
        assert all(q["course"] == course for q in p["questions"])
        prompts = [q["prompt"] for q in p["questions"]]
        assert len(prompts) == len(set(prompts))
        for q in p["questions"]:
            assert "answer" not in q
            assert "explain" not in q
            assert "hints" not in q


def test_grade_paper_full_and_empty():
    p = ma.new_paper("probability", count=10, seed=7)
    paper = ma._PAPERS[p["paper_id"]]
    answers = [{"qid": q["id"], "value": q["answer"], "elapsed_ms": 1000}
               for q in paper["questions"]]
    out = ma.grade_paper(p["paper_id"], answers, 10000)
    assert out["correct"] == out["total"]
    assert out["score"] == 100
    assert out["by_skill"]
    for row in out["questions"]:
        key = row["explain"].strip(" 。；;.")
        assert all(s.strip(" 。；;.") not in key for s in row["steps"])
        if row["kind"] == "tf":
            assert row["answer"] in ("正确", "错误")
        if row["kind"] == "counter":
            assert row["answer"] in [o["text"] for o in row["options"]]

    p2 = ma.new_paper("probability", count=10, seed=8)
    out2 = ma.grade_paper(p2["paper_id"], [], 0)
    assert out2["correct"] == 0
    assert out2["score"] == 0
    assert out2["total"] >= 5


def test_new_paper_rejects_unknown_course():
    with pytest.raises(ma.MathError):
        ma.new_paper("no-such-course", count=10)


# ---------------- 证明题 AI 步骤评分 ----------------

def test_proof_meta_hides_outline():
    out = ma.proof_meta("analysis1")
    assert out["proofs"]
    assert all("outline" not in p for p in out["proofs"])
    assert all(p["prompt"] and p["skill"] for p in out["proofs"])


def test_grade_proof_fallback(monkeypatch):
    import app.interview as interview
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: None)
    out = ma.grade_proof(
        "pf_analysis_sqrt2",
        "反设根号 2 等于 p/q，平方后推出 p 和 q 都为偶数，与互素矛盾。")
    assert out["degraded"] is True
    assert out["score"] is None
    assert out["outline"]


def test_grade_proof_ai(monkeypatch):
    import app.interview as interview
    monkeypatch.setattr(interview, "_llm_json", lambda *a, **k: {
        "score": 85, "verdict": "基本通过",
        "steps": [{"point": "反证", "status": "有", "comment": "思路正确"}],
        "missing": [], "suggestions": ["补一句 p、q 互素的理由"], "confidence": "高",
    })
    out = ma.grade_proof(
        "pf_analysis_sqrt2",
        "反设根号 2 等于 p/q，平方后推出 p 和 q 都为偶数，与互素矛盾。")
    assert out["degraded"] is False
    assert out["score"] == 85
    assert out["steps"]


def test_grade_proof_too_short():
    with pytest.raises(ma.MathError):
        ma.grade_proof("pf_analysis_sqrt2", "反证")
