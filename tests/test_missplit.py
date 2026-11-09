# -*- coding: utf-8 -*-
"""未命中构成：把 prompt-cached 拆成 首请求 / 新增 / 折叠重建 / 其他漂移。"""

from app import missplit


def test_empty_returns_zeros():
    out = missplit.split([])
    assert out == {"cold": 0, "new": 0, "rebuild": 0, "drift": 0, "total": 0, "rounds": 0}


def test_missing_fields_do_not_raise():
    out = missplit.split([{}, {"prompt": None}, "坏数据"])
    assert out["total"] == 0
    assert out["rounds"] == 0


def test_cold_turn_counts_as_cold():
    # 会话的第一次请求：没有可比前缀，整段 miss 归 cold
    out = missplit.split([{"n": 1, "prompt": 10000, "cached": 0, "cold": True}])
    assert out["cold"] == 10000
    assert out["new"] == 0 and out["drift"] == 0
    assert out["total"] == 10000


def test_warm_first_round_splits_by_prefix():
    # 上一回合已有请求：前缀 8000，本次 10000 → 只有 2000 是必然新增
    out = missplit.split([{"n": 1, "prompt": 10000, "cached": 6000, "cold": False,
                           "prefix_tokens": 8000, "cur_tokens": 10000}])
    assert out["new"] == 2000
    assert out["drift"] == 2000      # 实际 miss 4000 > 新增 2000
    assert out["cold"] == 0


def test_warm_first_round_perfect_cache():
    out = missplit.split([{"n": 1, "prompt": 10000, "cached": 8000, "cold": False,
                           "prefix_tokens": 8000, "cur_tokens": 10000}])
    assert out["new"] == 2000
    assert out["drift"] == 0


def test_second_round_growth_is_new():
    rounds = [
        {"n": 1, "prompt": 10000, "cached": 0, "cold": True},
        {"n": 2, "prompt": 12000, "cached": 10000, "est": 12000},
    ]
    out = missplit.split(rounds)
    assert out["cold"] == 10000
    assert out["new"] == 2000        # 估算从 10000 涨到 12000
    assert out["drift"] == 0


def test_second_round_unexplained_is_drift():
    rounds = [
        {"n": 1, "prompt": 10000, "cached": 0, "cold": True},
        {"n": 2, "prompt": 12000, "cached": 6000, "est": 12000},   # miss 6000，只涨了 2000
    ]
    out = missplit.split(rounds)
    assert out["new"] == 2000
    assert out["drift"] == 4000


def test_second_round_after_fold_is_rebuild():
    rounds = [
        {"n": 1, "prompt": 10000, "cached": 0, "cold": True},
        {"n": 2, "prompt": 12000, "cached": 6000, "est": 12000, "fold": True},
    ]
    out = missplit.split(rounds)
    assert out["rebuild"] == 4000
    assert out["drift"] == 0


def test_estimation_bias_is_scaled():
    # 本地估算从 5000 涨到 6000（增量 1000），真实 prompt 10000 / 估算 6000 ≈ 1.67
    rounds = [
        {"n": 1, "prompt": 10000, "cached": 0, "cold": True, "est": 5000},
        {"n": 2, "prompt": 10000, "cached": 8000, "est": 6000},   # miss 2000
    ]
    out = missplit.split(rounds)
    assert out["new"] == 1667        # 1000 × 10000/6000
    assert out["drift"] == 333


def test_absurd_ratio_falls_back_to_raw_growth():
    # 估算比例 16.7 远超合理范围：放弃校准，按原始增量 1000 算
    rounds = [
        {"n": 1, "prompt": 100000, "cached": 0, "cold": True, "est": 5000},
        {"n": 2, "prompt": 100000, "cached": 98000, "est": 6000},  # miss 2000
    ]
    out = missplit.split(rounds)
    assert out["new"] == 1000
    assert out["drift"] == 1000


def test_bias_scaling_capped_by_miss():
    rounds = [
        {"n": 1, "prompt": 12000, "cached": 0, "cold": True, "est": 6000},
        {"n": 2, "prompt": 12000, "cached": 9000, "est": 8000},
    ]
    out = missplit.split(rounds)
    # 增长 2000 × 2 = 4000，miss 3000 → new 封顶 3000，drift 0
    assert out["new"] == 3000
    assert out["drift"] == 0


def test_first_valid_round_used_when_earlier_round_has_no_usage():
    # 失败轮没有 prompt：不能把它当成「首轮」而让真正的第一轮落到 drift 分支
    rounds = [
        {"n": 1, "prompt": 0, "cached": 0},
        {"n": 2, "prompt": 10000, "cached": 8000, "cold": True},
    ]
    out = missplit.split(rounds)
    assert out["rounds"] == 1
    assert out["cold"] == 2000
    assert out["drift"] == 0


def test_total_equals_sum_of_parts():
    rounds = [
        {"n": 1, "prompt": 10000, "cached": 0, "cold": True},
        {"n": 2, "prompt": 12000, "cached": 6000, "est": 12000},
        {"n": 3, "prompt": 13000, "cached": 5000, "est": 13000, "fold": True},
    ]
    out = missplit.split(rounds)
    assert out["cold"] + out["new"] + out["rebuild"] + out["drift"] == out["total"]
