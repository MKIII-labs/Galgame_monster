# -*- coding: utf-8 -*-
"""全局规则：多条 + 单条开关（profile.rules），并兼容旧的单条 manual。"""
from app.profile import (DEFAULT_PROFILE, MAX_RULES, MAX_RULE_CHARS, clean_rules,
                         manual_block)


def test_default_profile_has_rules():
    assert DEFAULT_PROFILE["rules"] == []
    assert DEFAULT_PROFILE["manual"] == ""


def test_manual_block_falls_back_to_legacy_manual():
    assert manual_block({"manual": "  旧规则  "}) == "旧规则"
    assert manual_block({"manual": "旧规则", "rules": []}) == "旧规则"


def test_manual_block_joins_only_enabled_rules():
    p = {"manual": "旧规则", "rules": [
        {"text": "第一条", "enabled": True},
        {"text": "第二条", "enabled": False},
        {"text": "   ", "enabled": True},
        {"text": "第三条", "enabled": True},
    ]}
    assert manual_block(p) == "第一条\n\n第三条"


def test_manual_block_all_disabled_does_not_fall_back():
    """全部停用时必须真的为空，不能回退到 manual 把停用内容又塞回去。"""
    p = {"manual": "旧规则", "rules": [{"text": "A", "enabled": False}]}
    assert manual_block(p) == ""


def test_clean_rules_accepts_strings_and_coerces_enabled():
    out = clean_rules(["纯字符串", {"text": "  两条  ", "enabled": "false"}, {"text": ""}])
    assert [x["text"] for x in out] == ["纯字符串", "两条"]
    assert out[0]["enabled"] is True
    assert out[1]["enabled"] is False
    assert all(x["id"] for x in out)


def test_clean_rules_skips_bad_items_and_caps_limits():
    out = clean_rules([None, 3, {"text": "ok"}])
    assert [x["text"] for x in out] == ["ok"]

    long = clean_rules([{"text": "x" * (MAX_RULE_CHARS + 10)}])
    assert len(long[0]["text"]) == MAX_RULE_CHARS

    many = clean_rules([{"text": str(i)} for i in range(MAX_RULES + 5)])
    assert len(many) == MAX_RULES
