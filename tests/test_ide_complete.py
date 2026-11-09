# -*- coding: utf-8 -*-
"""IDE 内联补全：提示构造、结果清洗与限流"""
from app import ide_complete as ic
from app.registry import snapshot


def test_clean_completion_strips_fence():
    assert ic.clean_completion("```python\nprint(1)\n```") == "print(1)"
    assert ic.clean_completion("```\n```") == ""


def test_clean_completion_dedupes_suffix():
    """模型把后文也重写了一遍：只保留真正要插入的部分。"""
    text = "return a + b;\nfunction next() {"
    suffix = "function next() {\n  return 1;\n}"
    assert ic.clean_completion(text, suffix=suffix) == "return a + b;"


def test_clean_completion_limits_lines_and_chars():
    text = "\n".join("line%d" % i for i in range(20))
    out = ic.clean_completion(text, max_lines=3)
    assert out.split("\n") == ["line0", "line1", "line2"]
    assert len(ic.clean_completion("x" * 900, max_chars=100)) == 100


def test_clean_completion_empty():
    assert ic.clean_completion("   \n") == ""
    assert ic.clean_completion("") == ""


def test_parse_candidates_splits_and_dedupes():
    raw = "foo()\n---CANDIDATE---\nbar()\n---CANDIDATE---\nfoo()\n---CANDIDATE---\nbaz()"
    assert ic.parse_candidates(raw, limit=2) == ["foo()", "bar()"]
    assert ic.parse_candidates(raw, limit=9) == ["foo()", "bar()", "baz()"]
    assert ic.parse_candidates("```python\nfoo()\n```", limit=2) == ["foo()"]
    assert ic.parse_candidates("  \n---CANDIDATE---\n  ", limit=2) == []


def test_parse_candidates_keeps_first_recommended():
    raw = "return a\n---CANDIDATE---\nreturn b"
    out = ic.parse_candidates(raw, suffix="\nnext()", limit=2)
    assert out[0] == "return a"


def test_clean_completion_keeps_text_without_overlap():
    """后文没有重叠时不能误裁：短补全很容易被裁成空串。"""
    assert ic.clean_completion("print(x)", suffix="\n    return y\n") == "print(x)"
    assert ic.clean_completion("self.foo = bar",
                               suffix="\n    return y\n") == "self.foo = bar"


def test_complete_candidates_returns_list(monkeypatch):
    monkeypatch.setattr(ic, "load_config", lambda: {"light_model": "m"})
    monkeypatch.setattr(ic, "get_api_conf", lambda kind: ("k", "u", "m"))
    monkeypatch.setattr(ic, "_quick_chat",
                        lambda *a, **kw: "a = 1\n---CANDIDATE---\nb = 2")
    texts, why = ic.complete_candidates("x.py", "python", "x = 0\n", "")
    assert why == "" and texts == ["a = 1", "b = 2"]
    # 兼容入口仍然只返回第一个候选
    assert ic.complete("x.py", "python", "x = 0\n", "") == ("a = 1", "")


def test_build_messages_trims_context():
    msgs = ic.build_messages("a.py", "python", "x" * 10000, "y" * 10000)
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    user = msgs[1]["content"]
    assert "x" * ic.PREFIX_MAX in user          # 保留靠近光标的尾部
    assert "x" * (ic.PREFIX_MAX + 1) not in user
    assert "y" * ic.SUFFIX_MAX in user
    assert "y" * (ic.SUFFIX_MAX + 1) not in user
    assert "a.py" in user and "python" in user


def test_throttle_interval_and_cooldown():
    ic.reset_throttle()
    assert ic.allow("s1", now=100.0) is True
    assert ic.allow("s1", now=100.1) is False
    assert ic.allow("s1", now=101.0) is True
    for _ in range(ic.FAIL_LIMIT):
        ic.note_result("s1", False, now=200.0)
    assert ic.allow("s1", now=201.0) is False     # 冷却中
    assert ic.allow("s1", now=261.0) is True      # 冷却结束 + 间隔足够
    ic.reset_throttle()


def test_complete_refuses_without_light_model(monkeypatch):
    monkeypatch.setattr(ic, "load_config", lambda: {"light_model": ""})
    text, why = ic.complete("a.py", "python", "x = 1", "")
    assert text == "" and "轻量模型" in why


def test_complete_refuses_when_switched_off(monkeypatch):
    monkeypatch.setattr(ic, "load_config",
                        lambda: {"light_model": "m", "ide_ai_complete": False})
    text, why = ic.complete("a.py", "python", "x = 1", "")
    assert text == "" and "关闭" in why


def test_route_registered():
    from app.api import sandbox  # noqa: F401  导入时自注册路由
    assert ("POST", "/api/ide/complete") in snapshot()
    assert ("POST", "/api/ide/review") in snapshot()
