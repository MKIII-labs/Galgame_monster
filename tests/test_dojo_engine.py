# -*- coding: utf-8 -*-
"""研究道场：训练会话与提交。"""
import pytest

from app.dojo import engine


@pytest.fixture(autouse=True)
def _clean():
    engine.reset_store()
    yield
    engine.reset_store()


def test_start_follows_first_unit_and_hides_expert():
    out = engine.start("cmc-calculus")
    assert out["unit"]["id"] == "cmc-lim-01"
    assert "expert" not in out["unit"]
    assert out["pack"]["unit_count"] == 12
    assert len(out["pack"]["unit_index"]) == 12


def test_answer_then_reject_repeat():
    out = engine.start("cmc-calculus", "cmc-lim-01")
    r = engine.answer(out["session_id"], "cmc-lim-01", "C")
    assert r["scored"] is True and r["correct"] is True
    assert r["expert"]["why"]
    assert r["card"]["title"] == "相减先看阶"
    with pytest.raises(engine.DojoError):
        engine.answer(out["session_id"], "cmc-lim-01", "C")


def test_wrong_answer_can_retry_after_format_error():
    out = engine.start("cmc-calculus", "cmc-lim-01")
    with pytest.raises(engine.DojoError):
        engine.answer(out["session_id"], "cmc-lim-01", "Z")
    r = engine.answer(out["session_id"], "cmc-lim-01", "A")
    assert r["correct"] is False


def test_unknown_pack():
    with pytest.raises(engine.DojoError):
        engine.start("no-such-pack")


def test_missing_session():
    with pytest.raises(engine.DojoError):
        engine.answer("nope", "cmc-lim-01", "A")


def test_unit_mismatch():
    out = engine.start("cmc-calculus", "cmc-lim-01")
    with pytest.raises(engine.DojoError):
        engine.answer(out["session_id"], "cmc-ode-01", "A")


def test_l3_unit_returns_diagnosis_without_score():
    out = engine.start("cmc-calculus", "cmc-series-02")
    r = engine.answer(out["session_id"], "cmc-series-02",
                      "莱布尼茨判别法说明收敛；但绝对值是 p=1/2 的 p 级数，发散，所以条件收敛")
    assert r["scored"] is False
    assert r["score"] == 0.0
    assert r["judge"] == "L3"
    assert r["detail"]["coverage"] == 1.0
