# -*- coding: utf-8 -*-
"""P2 引用语义校验：JSON 解析 / 可注入 chat / claim-evidence 提取。"""
from app import research, search_verify


def test_parse_verdicts_tolerates_fence_and_string_bool():
    raw = '```json\n[{"i":1,"ok":true,"why":"x"},{"i":2,"ok":"false","why":"y"}]\n```'
    out = search_verify.parse_verdicts(raw, 2)
    assert len(out) == 2
    assert out[0]["ok"] is True
    assert out[1]["ok"] is False


def test_verify_with_injected_chat():
    seen = {}

    def chat(messages, max_tokens=600):
        seen["messages"] = messages
        return '[{"i":1,"ok":false,"why":"原文没有这个价格"}]'

    out = search_verify.verify(
        [{"i": 1, "claim": "价格是 120000", "evidence": "价格是 100000"}],
        chat=chat)
    assert out and out[0]["ok"] is False
    assert seen["messages"][0]["role"] == "system"


def test_claim_evidence_extracts_pairs(monkeypatch, tmp_path):
    monkeypatch.setattr(research, "DATA_DIR", str(tmp_path))
    led = research.Ledger("ce")
    led.add_sources([{
        "title": "T", "url": "https://e.com/1",
        "evidence": [{"quote": "价格是 100000 美元", "line_start": 1, "line_end": 1}],
    }])
    pairs = led.claim_evidence("据 [1]，价格是 100000 美元。")
    assert pairs
    assert pairs[0]["n"] == 1
    assert "100000" in pairs[0]["evidence"]
