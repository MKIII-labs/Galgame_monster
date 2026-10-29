# -*- coding: utf-8 -*-
"""自动化提问策略：三档分类与自决。"""
from app import autonomy_policy as qp


def test_normalize_mode():
    assert qp.normalize_mode("AUTO") == "auto"
    assert qp.normalize_mode("ask") == "ask"
    assert qp.normalize_mode("smart") == "smart"
    assert qp.normalize_mode("nonsense") == "smart"
    assert qp.normalize_mode(None) == "smart"


def test_classify_high_risk():
    assert qp.classify("要删除旧数据吗？")[0] == "high"
    assert qp.classify("是否强制覆盖原文件？")[0] == "high"
    assert qp.classify("要发布到生产环境吗？")[0] == "high"
    assert qp.classify("用你的密码登录吗？")[0] == "high"


def test_classify_missing_info():
    assert qp.classify("请提供你的 API 密钥")[0] in ("high", "info")
    assert qp.classify("需要用哪个账号登录？")[0] == "info"
    assert qp.classify("缺少外部数据，能先给我吗？")[0] == "info"


def test_classify_preference_low():
    assert qp.classify("配色用深色还是浅色？", ["深色", "浅色"])[0] == "low"
    assert qp.classify("用哪个技术栈？", ["React", "Vue"])[0] == "low"


def test_decide_modes(monkeypatch):
    monkeypatch.setattr(qp, "_llm_answer", lambda *a, **k: "深色")

    d = qp.decide("配色用深色还是浅色？", ["深色", "浅色"], mode="smart")
    assert d["action"] == "auto" and d["answer"] == "深色"

    d = qp.decide("要删除旧数据吗？", mode="smart")
    assert d["action"] == "ask" and not d["answer"]

    d = qp.decide("要删除旧数据吗？", mode="auto")
    assert d["action"] == "auto" and "不可逆" in d["answer"]

    d = qp.decide("配色用深色还是浅色？", mode="ask")
    assert d["action"] == "ask"


def test_fallback_answer_prefers_first_option(monkeypatch):
    monkeypatch.setattr(qp, "_llm_answer",
                        lambda q, opts=None, *a, **k: qp._fallback_answer(opts))
    assert qp.decide("选哪个？", ["A", "B"], mode="auto")["answer"] == "A"
    assert "最小可逆" in qp.decide("怎么做？", mode="auto")["answer"]
