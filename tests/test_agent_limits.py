# -*- coding: utf-8 -*-
"""Agent 执行上限：默认值、越界收敛与三处实际生效点"""
from app import agent


def test_limits_defaults_match_constants():
    lim = agent.limits()
    assert lim["max_rounds"] == agent.MAX_ROUNDS
    assert lim["repeat_limit"] == agent.REPEAT_LIMIT
    assert lim["cmd_timeout"] == agent.CMD_DEFAULT_TIMEOUT


def test_limits_empty_config_uses_defaults():
    assert agent.limits({}) == agent.limits()


def test_undo_limit_reads_setting():
    """撤销快照条数同样是可调参数：0 = 不限，负数回落默认。"""
    assert agent.undo_limit({"agent_max_undo": 0}) == 0
    assert agent.undo_limit({"agent_max_undo": 12}) == 12
    assert agent.undo_limit({"agent_max_undo": 9999}) == 9999   # 不设天花板
    assert agent.undo_limit({"agent_max_undo": -3}) == agent.MAX_UNDO
    assert agent.undo_limit({"agent_max_undo": "abc"}) == agent.MAX_UNDO
    assert agent.undo_limit({}) == agent.MAX_UNDO


def test_limits_reads_config():
    lim = agent.limits({
        "agent_max_rounds": 40,
        "agent_repeat_limit": 5,
        "agent_cmd_timeout": 300,
    })
    assert lim == {"max_rounds": 40, "repeat_limit": 5, "cmd_timeout": 300,
                   # 两个字符串档位：默认都不改变原有行为
                   "shell_mode": "strict", "formatter": "off",
                   # Code Mode 预算与写入开关
                   "code_max_calls": 2000, "code_parallel": 16,
                   "code_allow_write": False, "code_allow_cmd": False,
                   # 写前读默认开启（dsh 的 fs-policy 行为）
                   "require_read": True}


def test_limits_clamps_out_of_range():
    # 只守最小值，不设天花板：填多大就是多大
    lim = agent.limits({"agent_max_rounds": 999, "agent_repeat_limit": 0,
                        "agent_cmd_timeout": 9999})
    assert lim["max_rounds"] == 999
    assert lim["repeat_limit"] == 0
    assert lim["cmd_timeout"] == 9999
    lim = agent.limits({"agent_max_rounds": 1})
    assert lim["max_rounds"] == 1


def test_limits_zero_means_unlimited():
    """三项都能填 0 = 不限：0 原样返回，由调用方解释成「不刹车」"""
    lim = agent.limits({"agent_max_rounds": 0, "agent_repeat_limit": 0,
                        "agent_cmd_timeout": 0})
    assert lim == {"max_rounds": 0, "repeat_limit": 0, "cmd_timeout": 0,
                   "shell_mode": "strict", "formatter": "off",
                   "code_max_calls": 2000, "code_parallel": 16,
                   "code_allow_write": False, "code_allow_cmd": False,
                   "require_read": True}


def test_limits_negative_falls_back_to_default():
    # 负数没有意义，别悄悄当成「不限」
    lim = agent.limits({"agent_max_rounds": -5, "agent_cmd_timeout": -1})
    assert lim["max_rounds"] == agent.MAX_ROUNDS
    assert lim["cmd_timeout"] == agent.CMD_DEFAULT_TIMEOUT


def test_limits_falls_back_on_bad_type():
    # 空串 / None / 非数字都回落默认，不能让 int() 抛出去打断整轮对话
    lim = agent.limits({"agent_max_rounds": "", "agent_repeat_limit": None,
                        "agent_cmd_timeout": "abc"})
    assert lim["max_rounds"] == agent.MAX_ROUNDS
    assert lim["repeat_limit"] == agent.REPEAT_LIMIT
    assert lim["cmd_timeout"] == agent.CMD_DEFAULT_TIMEOUT


def test_normalize_auto_continue():
    """0 = 关闭，1–10 = 次数，-1 = 无上限，非法值回落 3"""
    assert agent.normalize_auto_continue(0) == 0
    assert agent.normalize_auto_continue(5) == 5
    assert agent.normalize_auto_continue(99) == 10
    assert agent.normalize_auto_continue(-1) == -1
    assert agent.normalize_auto_continue(-7) == -1
    assert agent.normalize_auto_continue("3") == 3
    assert agent.normalize_auto_continue("") == 3
    assert agent.normalize_auto_continue(None) == 3


def test_judge_write_has_no_count_threshold(tmp_path):
    """自动审批不再按写入次数 / 替换行数转人工（判定只看动作风险）。"""
    root = str(tmp_path)
    args = {"path": "a.txt", "old": "1\n2\n3\n4\n5\n6\n7\n8", "new": "x"}
    assert agent.judge_write(root, "write_file", args, "ai")[0] == "allow"
    assert agent.judge_write(root, "edit_file", args, "ai")[0] == "allow"


def test_repeat_guard_none_uses_constant():
    state = {"sig": "", "n": 0}
    sig = agent.call_signature("read_file", {"path": "a.py"})
    for _ in range(agent.REPEAT_LIMIT - 1):
        assert agent.repeat_guard(state, sig, limit=None) is False
    assert agent.repeat_guard(state, sig, limit=None) is True


def test_repeat_guard_zero_never_intercepts():
    """0 = 不限：同名同参调多少次都放行"""
    state = {"sig": "", "n": 0}
    sig = agent.call_signature("read_file", {"path": "a.py"})
    for _ in range(20):
        assert agent.repeat_guard(state, sig, limit=0) is False


def test_run_tool_zero_cmd_timeout_means_unlimited(tmp_path, monkeypatch):
    """命令超时填 0 时传 -1 给工具，而不是回落成默认 60 秒"""
    seen = {}

    def fake_run_command(root, args):
        seen.update(args)
        return "ok", None

    monkeypatch.setitem(agent._HANDLERS, "run_command", fake_run_command)
    caps = agent.limits({"agent_cmd_timeout": 0})
    agent.run_tool(str(tmp_path), "run_command", {"argv": ["git", "status"]}, caps=caps)
    assert seen["timeout"] == -1


def test_run_tool_injects_cmd_timeout(tmp_path, monkeypatch):
    seen = {}

    def fake_run_command(root, args):
        seen.update(args)
        return "ok", None

    monkeypatch.setitem(agent._HANDLERS, "run_command", fake_run_command)
    caps = agent.limits({"agent_cmd_timeout": 30})
    agent.run_tool(str(tmp_path), "run_command", {"argv": ["git", "status"]}, caps=caps)
    assert seen["timeout"] == 30


def test_run_tool_keeps_model_timeout(tmp_path, monkeypatch):
    seen = {}

    def fake_run_command(root, args):
        seen.update(args)
        return "ok", None

    monkeypatch.setitem(agent._HANDLERS, "run_command", fake_run_command)
    caps = agent.limits({"agent_cmd_timeout": 30})
    agent.run_tool(str(tmp_path), "run_command",
                   {"argv": ["git", "status"], "timeout": 5}, caps=caps)
    assert seen["timeout"] == 5
