# -*- coding: utf-8 -*-
"""P2 离线检索评测：金标集不联网，跑排序 + 引用校验指标。"""
import tempfile

from app import search_eval


def test_golden_eval_meets_thresholds():
    cases = search_eval.load_cases(search_eval.default_golden_path())
    assert len(cases) >= 8
    with tempfile.TemporaryDirectory() as td:
        result = search_eval.evaluate(cases, data_dir=td)
    m = result["metrics"]
    assert m["domain_hit"] >= 0.8
    assert m["citation_ok"] == 1.0
    assert m["unsupported_ok"] >= 0.9
    assert m["avg_diversity"] >= 0.6
