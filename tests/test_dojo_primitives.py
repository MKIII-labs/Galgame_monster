# -*- coding: utf-8 -*-
"""研究道场：8 个训练原语的判分。"""
import pytest

from app.dojo import primitives as p


def _unit(primitive, payload, expert, judge="L2"):
    return {"primitive": primitive, "judge": judge, "payload": payload, "expert": expert}


def test_single_choice():
    u = _unit("single_choice",
              {"options": [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}]},
              {"choice": "B"})
    assert p.judge(u, "B")["correct"] is True
    bad = p.judge(u, "A")
    assert bad["correct"] is False and bad["score"] == 0.0
    with pytest.raises(p.JudgeError):
        p.judge(u, "Z")


def test_multi_select_penalizes_wrong_picks():
    u = _unit("multi_select",
              {"options": [{"key": "A"}, {"key": "B"}, {"key": "C"}]},
              {"choice": ["A", "C"]})
    assert p.judge(u, ["C", "A"])["score"] == 1.0
    part = p.judge(u, ["A", "B"])          # 命中 1、错选 1、应有 2 → (1-0.5)/2
    assert part["correct"] is False
    assert part["score"] == pytest.approx(0.25)
    with pytest.raises(p.JudgeError):
        p.judge(u, ["A", "D"])


def test_order_partial_credit():
    u = _unit("order",
              {"steps": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]},
              {"order": ["s1", "s2", "s3"]})
    assert p.judge(u, ["s1", "s2", "s3"])["score"] == 1.0
    part = p.judge(u, ["s2", "s1", "s3"])  # 3 对里对 2 对
    assert part["score"] == pytest.approx(2 / 3)
    with pytest.raises(p.JudgeError):
        p.judge(u, ["s1", "s2"])           # 少排一步


def test_pair():
    u = _unit("pair",
              {"left": [{"id": "l1"}, {"id": "l2"}], "right": [{"id": "r1"}, {"id": "r2"}]},
              {"pairs": {"l1": "r1", "l2": "r2"}})
    assert p.judge(u, {"l1": "r1", "l2": "r2"})["score"] == 1.0
    half = p.judge(u, {"l1": "r2", "l2": "r2"})
    assert half["score"] == 0.5 and half["correct"] is False


def test_numeric_tolerance():
    u = _unit("numeric", {}, {"value": -1 / 6, "tolerance": 0.01}, judge="L1")
    assert p.judge(u, "-0.1666667")["correct"] is True
    assert p.judge(u, "-0.2")["correct"] is False
    with pytest.raises(p.JudgeError):
        p.judge(u, "不是数")


def test_expr_equivalence_and_whitelist():
    u = _unit("expr", {"var": "x"}, {"expr": "x^2 + 1"}, judge="L1")
    assert p.judge(u, "1 + x**2")["correct"] is True
    assert p.judge(u, "x^2 + 2")["correct"] is False
    with pytest.raises(p.JudgeError):
        p.judge(u, "__import__('os').system('echo hi')")


def test_annotate_f1():
    u = _unit("annotate",
              {"segments": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]},
              {"segments": ["s1", "s3"]})
    assert p.judge(u, ["s3", "s1"])["correct"] is True
    one = p.judge(u, ["s1"])               # 精确率 1、召回 0.5 → F1 2/3
    assert one["score"] == pytest.approx(2 / 3)
    assert p.judge(u, [])["score"] == 0.0


def test_short_text_is_diagnosis_only():
    u = _unit("short_text", {},
              {"rubric": [
                  {"id": "r1", "keywords": ["莱布尼茨"], "weight": 1},
                  {"id": "r2", "keywords": ["条件收敛"], "weight": 1},
              ]}, judge="L3")
    r = p.judge(u, "由莱布尼茨判别法可知收敛，所以条件收敛")
    assert r["scored"] is False and r["score"] == 0.0
    assert r["detail"]["coverage"] == 1.0
    assert r["detail"]["missing"] == []


def test_l3_never_scores_even_for_choice():
    u = _unit("single_choice", {"options": [{"key": "A"}, {"key": "B"}]},
              {"choice": "A"}, judge="L3")
    r = p.judge(u, "A")
    assert r["scored"] is False and r["score"] == 0.0
