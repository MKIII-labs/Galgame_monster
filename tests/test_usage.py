# -*- coding: utf-8 -*-
"""用量流水：端点溯源、usage 归一化与按端点聚合"""
import time

import pytest

from app import usage


@pytest.fixture
def usage_file(tmp_path, monkeypatch):
    """把流水文件指到临时目录，避免污染真实的 data/token_usage.jsonl"""
    f = tmp_path / "token_usage.jsonl"
    monkeypatch.setattr(usage, "USAGE_FILE", str(f))
    return f


def test_rounds_for_turn_matches_end_time(usage_file):
    usage.record_usage({
        "ts": 1000.0, "source": "chat",
        "timing": {"rounds": 2,
                   "rounds_detail": [{"n": 1, "prompt": 4},
                                     {"n": 2, "prompt": 6}]},
    })
    rd = usage.rounds_for_turn(1000.2, 2)
    assert [r["prompt"] for r in rd] == [4, 6]
    assert usage.rounds_for_turn(1000.2, 3) == []
    assert usage.rounds_for_turn(2000, 2) == []


def test_rounds_for_turn_skips_other_session(usage_file):
    usage.record_usage({
        "ts": 3000.0, "source": "chat", "session_id": "s_a",
        "timing": {"rounds": 1, "rounds_detail": [{"n": 1, "prompt": 9}]},
    })
    assert usage.rounds_for_turn(3000.1, 1, session_id="s_b") == []
    assert usage.rounds_for_turn(3000.1, 1, session_id="s_a")[0]["prompt"] == 9


def test_endpoint_of():
    assert usage.endpoint_of("https://api.deepseek.com/chat/completions") == "api.deepseek.com"
    assert usage.endpoint_of("HTTPS://DashScope.Aliyuncs.com/api/v1") == "dashscope.aliyuncs.com"
    assert usage.endpoint_of("") == usage.UNKNOWN_ENDPOINT
    assert usage.endpoint_of("not a url") == usage.UNKNOWN_ENDPOINT


def test_usage_counts_both_protocols():
    # OpenAI 兼容
    assert usage.usage_counts({"prompt_tokens": 10, "completion_tokens": 3,
                               "prompt_cache_hit_tokens": 4}) == {
        "prompt": 10, "completion": 3, "total": 13, "cached": 4}
    # DashScope 原生
    assert usage.usage_counts({"input_tokens": 7, "output_tokens": 2,
                               "total_tokens": 9}) == {
        "prompt": 7, "completion": 2, "total": 9, "cached": 0}
    assert usage.usage_counts({}) is None
    assert usage.usage_counts(None) is None


def test_record_llm_usage_traces_endpoint(usage_file):
    usage.record_llm_usage({"prompt_tokens": 10, "completion_tokens": 5}, "m1", "chat",
                           api="chat", url="https://api.deepseek.com/v1/chat/completions")
    usage.record_llm_usage({"input_tokens": 4, "output_tokens": 1}, "m2", "vision",
                           api="vision", url="https://dashscope.aliyuncs.com/api/v1/x")

    data = usage.aggregate_usage("day", 7)
    eps = {r["endpoint"]: r for r in data["by_endpoint"]}
    assert eps["api.deepseek.com"]["total"] == 15
    assert eps["api.deepseek.com"]["calls"] == 1
    assert eps["dashscope.aliyuncs.com"]["total"] == 5
    assert data["summary"]["total"] == 20
    assert data["calls_total"] == 2

    by_api = {c["api"] for c in data["calls"]}
    assert by_api == {"chat", "vision"}
    assert data["calls"][0]["ts"] >= data["calls"][1]["ts"]      # 最新在前


def test_aggregate_legacy_rows(usage_file):
    """旧流水没有 api / endpoint：按 chat + 未记录端点兼容，不能丢数据"""
    usage.record_usage({"ts": time.time(), "source": "chat", "model": "m",
                        "prompt": 1, "completion": 2, "total": 3})
    data = usage.aggregate_usage("day", 7)
    assert data["by_endpoint"][0]["endpoint"] == usage.UNKNOWN_ENDPOINT
    assert data["by_endpoint"][0]["total"] == 3
    assert data["calls"][0]["api"] == "chat"


def test_call_limit(usage_file):
    for i in range(5):
        usage.record_llm_usage({"prompt_tokens": 1, "completion_tokens": 1}, "m", "chat",
                               api="chat", url="https://api.deepseek.com/v1/chat/completions")
    data = usage.aggregate_usage("day", 7, call_limit=2)
    assert len(data["calls"]) == 2
    assert data["calls_total"] == 5


def test_aggregate_daily_buckets(usage_file):
    """daily 覆盖连续时间桶（空白日补 0），且各项之和等于 summary"""
    now = time.time()
    usage.record_usage({"ts": now, "source": "chat", "api": "chat", "endpoint": "e",
                        "model": "m", "prompt": 100, "completion": 20,
                        "total": 120, "cached": 60})
    usage.record_usage({"ts": now, "source": "chat", "api": "chat", "endpoint": "e",
                        "model": "m", "prompt": 100, "completion": 10,
                        "total": 110, "cached": 40})
    data = usage.aggregate_usage("day", 7)

    daily = data["daily"]
    assert len(daily) == 7 == len(data["labels"])
    assert [r["bucket"] for r in daily] == data["labels"]          # 与折线图共用横轴
    today = daily[-1]
    assert (today["prompt"], today["completion"], today["total"],
            today["cached"], today["calls"]) == (200, 30, 230, 100, 2)
    assert sum(r["total"] for r in daily) == data["summary"]["total"]
    assert sum(r["calls"] for r in daily) == data["summary"]["calls"]


def test_aggregate_derived(usage_file):
    now = time.time()
    usage.record_usage({"ts": now, "source": "chat", "api": "chat", "endpoint": "e",
                        "model": "m", "prompt": 100, "completion": 20,
                        "total": 120, "cached": 60})
    usage.record_usage({"ts": now, "source": "chat", "api": "chat", "endpoint": "e",
                        "model": "m", "prompt": 100, "completion": 10,
                        "total": 110, "cached": 40})
    d = usage.aggregate_usage("day", 7)["derived"]
    assert d["hit_rate"] == 50.0        # 100 / 200 输入
    assert d["avg_call"] == 115         # 230 / 2 次
    assert d["avg_active"] == 230       # 只按有消耗的 1 天
    assert d["active"] == 1
    assert d["peak"]["total"] == 230
    assert d["peak"]["bucket"] == usage._bucket_keys("day", 7)[-1]


def test_aggregate_derived_empty(usage_file):
    """空区间不能除零：命中率给 None，其余给 0 / 空峰值"""
    d = usage.aggregate_usage("day", 7)["derived"]
    assert d["hit_rate"] is None
    assert d["avg_call"] == 0 and d["avg_active"] == 0
    assert d["active"] == 0 and d["peak"] is None


def test_aggregate_by_api(usage_file):
    now = time.time()
    usage.record_usage({"ts": now, "source": "chat", "api": "chat", "endpoint": "e",
                        "model": "m", "prompt": 10, "completion": 1, "total": 11})
    usage.record_usage({"ts": now, "source": "char_profile", "api": "light",
                        "endpoint": "e", "model": "s", "prompt": 5,
                        "completion": 1, "total": 6})
    rows = usage.aggregate_usage("day", 7)["by_api"]
    assert [r["api"] for r in rows] == ["chat", "light"]     # 按 total 倒序
    assert rows[0]["total"] == 11 and rows[1]["calls"] == 1

def test_aggregate_call_stats_and_obs(usage_file):
    """批 1 观测：单次调用分布 + 前缀/折叠事件必须能从流水里还原"""
    now = time.time()
    usage.record_usage({
        "ts": now, "source": "chat", "prompt": 100, "completion": 10,
        "total": 110, "cached": 80, "session_id": "s_a",
        "timing": {
            "rounds_detail": [{"prompt": 10}, {"prompt": 20}, {"prompt": 30}],
            "obs": {
                "fold": 2, "fold_removed": 5, "compress": 1, "overflow": 0,
                "prefix": {"cold": False, "prefix_ratio": 0.8, "reuse_ratio": 0.7,
                           "sys_changed": True},
                "budget": {"level": 2},
            },
        },
    })
    data = usage.aggregate_usage("day", 30)
    cs = data["call_stats"]
    assert cs["count"] == 3
    assert cs["p50"] == 20
    assert cs["max"] == 30
    obs = data["obs_stats"]
    assert obs["turns"] == 1
    assert obs["warm"] == 1
    assert obs["sys_changed"] == 1
    assert obs["fold"] == 2
    assert obs["compress"] == 1
    assert obs["levels"] == {"2": 1}


def test_aggregate_call_stats_empty(usage_file):
    data = usage.aggregate_usage("day", 30)
    assert data["call_stats"]["count"] == 0
    assert data["obs_stats"]["turns"] == 0
    assert data["obs_stats"]["avg_prefix_ratio"] is None
    assert data["obs_stats"]["miss_parts"] == {
        "cold": 0, "new": 0, "rebuild": 0, "drift": 0, "total": 0, "turns": 0}


def test_aggregate_miss_parts(usage_file):
    """未命中构成：多条回合累加；没有 miss 字段的旧记录只影响 turns，不污染拆解"""
    now = time.time()
    usage.record_usage({
        "ts": now, "source": "chat", "prompt": 200, "completion": 1, "total": 201,
        "cached": 0,
        "timing": {"obs": {"miss": {"cold": 100, "new": 50, "rebuild": 20,
                                    "drift": 30, "total": 200, "rounds": 3}}},
    })
    usage.record_usage({
        "ts": now, "source": "chat", "prompt": 15, "completion": 1, "total": 16,
        "cached": 0,
        "timing": {"obs": {"miss": {"cold": 10, "new": 5, "rebuild": 0,
                                    "drift": 0, "total": 15, "rounds": 1}}},
    })
    usage.record_usage({
        "ts": now, "source": "chat", "prompt": 9, "completion": 1, "total": 10,
        "cached": 0, "timing": {"obs": {"fold": 1}},
    })
    mp = usage.aggregate_usage("day", 30)["obs_stats"]["miss_parts"]
    assert mp["turns"] == 2                    # 只有带拆解的回合
    assert (mp["cold"], mp["new"], mp["rebuild"], mp["drift"], mp["total"]) == (
        110, 55, 20, 30, 215)
    assert mp["cold"] + mp["new"] + mp["rebuild"] + mp["drift"] == mp["total"]
