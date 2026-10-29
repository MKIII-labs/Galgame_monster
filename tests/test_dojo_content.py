# -*- coding: utf-8 -*-
"""内置题库回归：标准答案必须判满分，且题型足够多样。"""
import pytest

from app.dojo import packs, primitives

NEW_PACKS = (
    "cumcm",
    "higher-algebra",
    "probability",
    "math-analysis",
    "ode",
    "complex-analysis",
    "abstract-algebra",
)


@pytest.fixture(autouse=True)
def _reset():
    packs.reset_cache()
    yield
    packs.reset_cache()


def _standard_answer(unit):
    expert = unit["expert"]
    primitive = unit["primitive"]
    if primitive == "single_choice":
        return expert["choice"]
    if primitive == "multi_select":
        return expert["choice"]
    if primitive == "order":
        return expert["order"]
    if primitive == "pair":
        return expert["pairs"]
    if primitive == "numeric":
        return expert["value"]
    return None


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_standard_answers_score_full(pack_id):
    pack = packs.get_pack(pack_id)
    train = [u for u in pack["units"] if not u.get("test")]
    assert len(train) >= 10
    for unit in train:
        answer = _standard_answer(unit)
        result = primitives.judge(unit, answer)
        assert result["scored"] is True, (pack_id, unit["id"])
        assert result["correct"] is True, (pack_id, unit["id"], result)
        assert result["score"] == 1.0, (pack_id, unit["id"], result)


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_pack_uses_at_least_three_primitives(pack_id):
    pack = packs.get_pack(pack_id)
    assert len({u["primitive"] for u in pack["units"]}) >= 3


@pytest.mark.parametrize("pack_id", NEW_PACKS)
def test_every_unit_has_card_and_reason(pack_id):
    pack = packs.get_pack(pack_id)
    for unit in pack["units"]:
        assert unit["expert"]["why"].strip()
        assert unit["expert"]["signals"]
        for field in ("title", "trigger", "first_step", "pitfall"):
            assert unit["card"][field].strip(), (pack_id, unit["id"], field)
