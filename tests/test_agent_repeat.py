# -*- coding: utf-8 -*-
"""重复调用守卫（doom loop）"""
from app import agent


def test_call_signature_is_order_insensitive():
    a = agent.call_signature("read_file", {"path": "a.py", "start": 1})
    b = agent.call_signature("read_file", {"start": 1, "path": "a.py"})
    assert a == b
    assert a != agent.call_signature("read_file", {"path": "b.py", "start": 1})
    assert a != agent.call_signature("write_file", {"path": "a.py", "start": 1})


def test_repeat_guard_blocks_fifth_identical():
    st = {"sig": "", "n": 0}
    sig = agent.call_signature("read_file", {"path": "a.py"})
    for _ in range(4):
        assert agent.repeat_guard(st, sig) is False   # 1–4
    assert agent.repeat_guard(st, sig) is True        # 5 → 拦
    assert agent.repeat_guard(st, sig) is True        # 6 仍拦


def test_repeat_guard_resets_on_different_call():
    st = {"sig": "", "n": 0}
    a = agent.call_signature("read_file", {"path": "a.py"})
    b = agent.call_signature("read_file", {"path": "b.py"})
    agent.repeat_guard(st, a)
    agent.repeat_guard(st, a)
    for _ in range(4):
        assert agent.repeat_guard(st, b) is False     # 换了文件 → 重新计数
    assert agent.repeat_guard(st, b) is True


def test_repeat_guard_custom_limit():
    st = {"sig": "", "n": 0}
    sig = agent.call_signature("run_command", {"argv": ["git", "status"]})
    assert agent.repeat_guard(st, sig, limit=2) is False
    assert agent.repeat_guard(st, sig, limit=2) is True
