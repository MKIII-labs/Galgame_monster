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


def test_window_hits_catches_interleaved_loop():
    """build → render → build → render：连续判据看不见，窗口判据要能抓到。"""
    st = {"hist": []}
    build = agent.call_signature("run_command", {"argv": ["python", "v3/build.py"]})
    render = agent.call_signature("run_command", {"argv": ["python", "v3/render.py"]})
    hits = []
    for _ in range(3):
        hits.append(agent.window_hits(st, build, window=24, limit=3))
        agent.window_hits(st, render, window=24, limit=3)
    assert hits == [False, False, True]


def test_window_hits_respects_window_size():
    st = {"hist": []}
    sig = agent.call_signature("read_file", {"path": "a.py"})
    for _ in range(3):
        # 窗口只留 2 条，永远凑不满 3 次
        assert agent.window_hits(st, sig, window=2, limit=3) is False


def test_reset_loop_window_clears_progress():
    st = {"hist": []}
    sig = agent.call_signature("run_command", {"argv": ["python", "build.py"]})
    agent.window_hits(st, sig, window=24, limit=3)
    agent.window_hits(st, sig, window=24, limit=3)
    agent.reset_loop_window(st)
    assert agent.window_hits(st, sig, window=24, limit=3) is False


def test_call_signature_tracks_script_change(tmp_path):
    p = tmp_path / "build.py"
    p.write_text("print(1)", encoding="utf-8")
    args = {"argv": ["python", "build.py"]}
    a = agent.call_signature("run_command", args, str(tmp_path))
    p.write_text("print(22222222)", encoding="utf-8")
    b = agent.call_signature("run_command", args, str(tmp_path))
    assert a != b      # 改了脚本再跑，不算重复调用


def test_result_ok_uses_exit_code():
    ok_text = "命令：python x.py\n工作目录：.\n结论：成功（退出码：0，用时 0.1s）\n输出："
    bad_text = "命令：python x.py\n工作目录：.\n结论：失败（退出码：1，用时 0.1s）\n输出：Traceback"
    assert agent.result_ok("run_command", ok_text) is True
    assert agent.result_ok("run_command", bad_text) is False
    assert agent.result_ok("read_file", bad_text) is True     # 非命令工具仍走 ❌ 约定
    assert agent.result_ok("run_command", "❌ 找不到命令") is False


def test_strip_cmd_noise_only_when_success():
    noisy = "Exception in module register(): x\n真正的一行输出"
    assert "Exception" not in agent._strip_cmd_noise(noisy, 0)
    assert "Exception" in agent._strip_cmd_noise(noisy, 1)    # 失败时保留全部输出
