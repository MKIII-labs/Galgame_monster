# -*- coding: utf-8 -*-
"""run_command：白名单 / 风险分级 / 沙箱 cwd / 环境清洗 / 超时"""
import sys
import time

from app import agent


# ---------------------------------------------------------------- 参数校验
def test_meta_chars_rejected():
    """字符串形式的 cmd 不做 shell 解析：含串联 / 重定向符号直接拒绝。

    注意用 ai 档：完全放权（full）已不再做这层校验，见 test_full_mode_is_unrestricted。
    """
    for bad in ('python -c "print(1) > out.txt"',
                'python -c "a && b"',
                "git status | more",
                'python -c "`whoami`"'):
        decision, why = agent.judge_command("", {"cmd": bad}, "ai")
        assert decision == "deny", bad
        assert "管道" in why or "串联" in why


def test_argv_keeps_symbols():
    """argv 不经过 shell，分号 / 大于号是普通参数，不能误杀。"""
    argv, err = agent._cmd_argv({"argv": ["python", "a.py", "x;y>z"]})
    assert err == "" and argv[-1] == "x;y>z"


def test_exe_whitelist_and_blacklist():
    assert agent.judge_command("", {"argv": ["powershell", "-c", "ls"]}, "ai")[0] == "deny"
    assert agent.judge_command("", {"argv": ["cmd", "/c", "dir"]}, "ai")[0] == "deny"
    assert agent.judge_command("", {"argv": ["curl", "http://x"]}, "ai")[0] == "deny"
    assert agent.judge_command("", {"argv": ["notepad", "a.txt"]}, "ai")[0] == "deny"
    assert agent.judge_command("", {"argv": []}, "full")[0] == "deny"


def test_run_command_unrestricted_executes_inline(tmp_path):
    """完全放权：内联执行不再被拦，能真的跑出结果。"""
    argv = [sys.executable, "-c", "print('ok-unrestricted')"]
    blocked, _u1, _w1 = agent.run_tool(str(tmp_path), "run_command", {"argv": argv})
    assert "内联执行" in blocked             # 常规档位仍然拦
    text, _u2, _w2 = agent.run_tool(str(tmp_path), "run_command", {"argv": argv},
                                    unrestricted=True)
    assert "ok-unrestricted" in text and "退出码：0" in text


# ---------------------------------------------------------------- 风险分级
def test_ai_mode_kinds(tmp_path):
    root = str(tmp_path)
    assert agent.judge_command(root, {"argv": ["git", "status"]}, "ai")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["git", "log", "-n", "5"]}, "ai")[0] == "allow"
    # 只改本地仓库的 git 子命令算中风险，直接放行
    assert agent.judge_command(root, {"argv": ["git", "add", "."]}, "ai")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["git", "commit", "-m", "x"]}, "ai")[0] == "allow"
    # 改远端历史 / 丢弃本地改动仍是高风险
    assert agent.judge_command(root, {"argv": ["git", "push"]}, "ai")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["git", "reset", "--hard"]}, "ai")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["npm", "install"]}, "ai")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["pip", "install", "rich"]}, "ai")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["python", "-m", "pip", "install", "rich"]},
                               "ai")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["python", "-m", "pytest"]}, "ai")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["python", "--version"]}, "ai")[0] == "allow"


def test_full_mode_still_asks_for_dangerous_git(tmp_path):
    """事故回归：完全放权下，覆盖工作区 / 改写历史的 git 命令仍要确认。

    背景：MC 测试里模型执行 `git checkout HEAD -- .`，把用户清空后的工作区
    整体恢复成 HEAD；当时会话是 full 档，命令被静默放行。
    """
    root = str(tmp_path)
    for argv in (["git", "checkout", "HEAD", "--", "."],
                 ["git", "checkout", "--", "."],
                 ["git", "restore", "."],
                 ["git", "reset", "--hard"],
                 ["git", "clean", "-fd"]):
        assert agent.judge_command(root, {"argv": argv}, "full")[0] == "ask", argv
        assert agent.judge_command(root, {"argv": argv}, "full",
                                   unrestricted=True)[0] == "ask", argv
        # 无人值守：直接拒绝，不弹一个没人看的审批
        assert agent.judge_command(root, {"argv": argv}, "full",
                                   interactive=False)[0] == "deny", argv

    # 字符串形式同样命中
    assert agent.judge_command(root, {"cmd": "git checkout HEAD -- ."}, "full")[0] == "ask"
    # 只读 / 中风险仍然直接放行
    assert agent.judge_command(root, {"argv": ["git", "status"]}, "full")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["git", "add", "."]}, "full")[0] == "allow"


def test_ai_mode_has_no_turn_count_limit(tmp_path):
    """自动审批按风险判定，不再按「本轮执行第几次」转人工。"""
    root = str(tmp_path)
    (tmp_path / "a.py").write_text("print(1)", encoding="utf-8")
    for _ in range(10):
        assert agent.judge_command(root, {"argv": ["python", "a.py"]}, "ai")[0] == "allow"


# ---------------------------------------------------------------- 换壳执行
# 这些校验针对常规档位（ai / custom）；完全放权（full）会全部跳过，见文件末尾。
def test_inline_execution_denied():
    """内联执行不落盘就能起任意 exe（曾用 python -c 绕开白名单），一律拒绝。"""
    for argv in (["python", "-c", "print(1)"],
                 ["python3", "-c", "print(1)"],
                 ["py", "-3", "-c", "print(1)"],
                 ["node", "-e", "console.log(1)"],
                 ["node", "--eval=1"],
                 ["node", "-p", "1"]):
        decision, why = agent.judge_command("", {"argv": argv}, "ai")
        assert decision == "deny", argv
        assert "内联" in why


def test_module_execution_restricted(tmp_path):
    root = str(tmp_path)
    assert agent.judge_command(root, {"argv": ["python", "-m", "http.server"]},
                               "ai")[0] == "deny"
    assert agent.judge_command(root, {"argv": ["python", "-m", "pytest"]},
                               "ai")[0] == "allow"
    # python -m pip 一律按「安装类」看待，即使是 list 也问一次
    assert agent.judge_command(root, {"argv": ["python", "-m", "pip", "list"]},
                               "ai")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["python", "-m"]}, "ai")[0] == "deny"


def test_script_must_be_inside_sandbox(tmp_path):
    root = str(tmp_path)
    (tmp_path / "ok.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "a.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    assert agent.judge_command(root, {"argv": ["python", "ok.py"]}, "ai")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["node", "a.js"]}, "ai")[0] == "allow"
    # 越界 / 不存在 / 扩展名不对 / 不给脚本：都拒绝
    assert agent.judge_command(root, {"argv": ["python", "../evil.py"]}, "ai")[0] == "deny"
    assert agent.judge_command(root, {"argv": ["python", "C:/Windows/evil.py"]},
                               "ai")[0] == "deny"
    assert agent.judge_command(root, {"argv": ["python", "missing.py"]}, "ai")[0] == "deny"
    assert agent.judge_command(root, {"argv": ["python", "x.txt"]}, "ai")[0] == "deny"
    assert agent.judge_command(root, {"argv": ["python"]}, "ai")[0] == "deny"
    # 版本查询不需要脚本
    assert agent.judge_command(root, {"argv": ["python", "--version"]}, "ai")[0] == "allow"


def test_ask_mode_always_asks(tmp_path):
    """手动审批档：只读命令也要问，保住档位的字面含义。"""
    assert agent.judge_command(str(tmp_path), {"argv": ["git", "status"]}, "ask")[0] == "ask"


def test_full_mode_is_unrestricted(tmp_path):
    """完全访问：白名单 / 内联执行 / 脚本路径校验都不再生效（cwd 仍锁沙箱）。

    例外：高危 git（push / checkout / reset / clean …）仍要审批，
    见 test_full_mode_still_asks_for_dangerous_git。
    """
    root = str(tmp_path)
    assert agent.judge_command(root, {"argv": ["git", "push"]}, "full")[0] == "ask"
    assert agent.judge_command(root, {"argv": ["powershell", "-c", "ls"]}, "full")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["python", "../evil.py"]}, "full")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["python", "-c", "print(1)"]},
                               "full")[0] == "allow"


def test_custom_command_rules(tmp_path):
    gm = tmp_path / ".gm"
    gm.mkdir()
    (gm / "agent-rules.json").write_text(
        '{"commands": {"allow": ["python -m pytest*"], "deny": ["git push*"]}}',
        encoding="utf-8")
    root = str(tmp_path)
    assert agent.judge_command(root, {"argv": ["python", "-m", "pytest", "-q"]},
                               "custom")[0] == "allow"
    assert agent.judge_command(root, {"argv": ["git", "push", "origin", "main"]},
                               "custom") == ("deny", "规则禁止：git push*")
    # 未命中规则：自定义档按「必须询问」兜底
    assert agent.judge_command(root, {"argv": ["git", "status"]}, "custom")[0] == "ask"


# ---------------------------------------------------------------- 真执行
def _mk_script(tmp_path, code, name="s.py"):
    """把要执行的代码落成沙箱内的脚本：内联 -c 已被禁止。"""
    (tmp_path / name).write_text(code, encoding="utf-8")
    return name


def test_resolve_argv_fills_cmd_extension(monkeypatch):
    """strict 在 Windows 上要把 npm 补成 npm.CMD，否则 Popen 找不到。"""
    monkeypatch.setattr(agent.shutil, "which",
                        lambda name: r"C:\Tools\npm.CMD" if name == "npm" else None)
    assert agent._resolve_argv(["npm", "install"]) == [r"C:\Tools\npm.CMD", "install"]
    assert agent._resolve_argv(["not-installed", "x"]) == ["not-installed", "x"]


def test_run_command_resolves_npm_cmd_if_available(tmp_path):
    """真实跑一次 npm --version：验证 strict 不再报「找不到命令」。"""
    if not agent.shutil.which("npm"):
        import pytest
        pytest.skip("npm 未安装")
    text, _u, _w = agent.run_tool(str(tmp_path), "run_command",
                                  {"argv": ["npm", "--version"]})
    assert "退出码：0" in text
    assert "找不到命令" not in text


def test_run_command_output(tmp_path):
    script = _mk_script(tmp_path, "print('ok')")
    text, undo, is_write = agent.run_tool(
        str(tmp_path), "run_command", {"argv": [sys.executable, script]})
    assert undo is None
    assert is_write is False
    assert "退出码：0" in text
    assert "ok" in text


def test_run_command_exit_code(tmp_path):
    script = _mk_script(tmp_path, "import sys; sys.exit(3)")
    text, _u, _w = agent.run_tool(
        str(tmp_path), "run_command", {"argv": [sys.executable, script]})
    assert "退出码：3" in text


def test_run_command_inline_code_rejected(tmp_path):
    text, _u, _w = agent.run_tool(
        str(tmp_path), "run_command", {"argv": [sys.executable, "-c", "print(1)"]})
    assert text.startswith("❌")
    assert "内联" in text


def test_run_command_cwd_escape_rejected(tmp_path):
    script = _mk_script(tmp_path, "print(1)")
    text, _u, _w = agent.run_tool(
        str(tmp_path), "run_command", {"argv": [sys.executable, script], "cwd": ".."})
    assert text.startswith("❌")


def test_run_command_timeout(tmp_path):
    script = _mk_script(tmp_path, "import time; time.sleep(30)")
    t0 = time.time()
    text, _u, _w = agent.run_tool(
        str(tmp_path), "run_command", {"argv": [sys.executable, script], "timeout": 1})
    assert "超时" in text
    assert time.time() - t0 < 20


def test_run_command_env_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("GM_TEST_API_KEY", "secret-value")
    script = _mk_script(
        tmp_path, "import os;print(os.environ.get('GM_TEST_API_KEY', 'MISSING'))")
    text, _u, _w = agent.run_tool(
        str(tmp_path), "run_command", {"argv": [sys.executable, script]})
    assert "MISSING" in text
    assert "secret-value" not in text
