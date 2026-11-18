# -*- coding: utf-8 -*-
"""持久 shell 的命令策略：非执行动作免审批 + 只读白名单 / 禁止清单 / 自主度。"""
from app.agent import approval as A


def _judge(cmd, mode="ai", **kw):
    kw.setdefault("autonomy", "whitelist")
    return A.judge_shell(mode, args={"action": "run", "command": cmd}, **kw)


def test_non_exec_actions_never_ask():
    for act in ("read", "status", "close", "interrupt"):
        assert A.judge_shell("ask", args={"action": act}) == ("allow", "")


def test_whitelist_allows_readonly_commands():
    for cmd in ("Get-Content a.txt",
                "Get-ChildItem . | Select-Object -First 5",
                "git status", "git diff --stat", "git log --oneline -5",
                "rg -n foo src", "findstr foo *.py",
                "python --version", "node -v"):
        decision, why = _judge(cmd)
        assert decision == "allow", (cmd, why)


def test_write_or_compound_still_asks():
    for cmd in ("Get-Content a.txt > out.txt",
                "Get-ChildItem . > list.txt",
                "Remove-Item x",
                "Get-ChildItem . | Remove-Item",
                "Get-ChildItem { $_ }",
                "Get-Content $env:TEMP\\x",
                "dir; Remove-Item x",
                "cat a && rm b",
                "Set-Content a.txt hello"):
        decision, why = _judge(cmd)
        assert decision == "ask", (cmd, decision)


def test_deny_list_wins_over_allow():
    decision, why = _judge("Get-Content a.txt", deny=["get-content*"])
    assert decision == "ask" and "禁止清单" in why


def test_custom_allow_list():
    decision, why = _judge("python -m pytest -q", allow=["python -m pytest*"])
    assert decision == "allow", why


def test_secret_path_asks():
    decision, why = _judge("Get-Content .env")
    assert decision == "ask" and "密钥" in why
    assert _judge("Get-Content id_rsa")[0] == "ask"


def test_autonomy_modes():
    assert _judge("Remove-Item x", autonomy="auto")[0] == "allow"
    assert _judge("Get-Content a.txt", autonomy="ask")[0] == "ask"
    assert _judge("Get-Content a.txt", mode="full")[0] == "allow"
    assert _judge("Get-Content a.txt", mode="custom")[0] == "ask"
    assert _judge("Get-Content a.txt", interactive=False)[0] == "deny"


def test_input_always_asks_in_whitelist():
    assert A.judge_shell("ai", args={"action": "input", "text": "ls"},
                         autonomy="whitelist")[0] == "ask"


def test_legacy_call_without_args_still_asks():
    assert A.judge_shell("ai")[0] == "ask"


def test_split_keeps_parenthesized_pipeline_together():
    cmd = ('Get-ChildItem D:\\ -Directory | ForEach-Object { '
           '$s=(Get-ChildItem $_.FullName -File | Measure-Object Length -Sum).Sum }')
    segs = A._split_shell_command(cmd)
    assert len(segs) == 2, segs
    assert segs[1].startswith("ForEach-Object") and segs[1].endswith("}")


def test_psdrive_is_readonly():
    assert _judge("Get-PSDrive D")[0] == "allow"
    assert _judge("get-psdrive")[0] == "allow"


def test_user_allow_can_cover_script_block_commands():
    cmd = 'Get-ChildItem D:\\ -Directory | ForEach-Object { $s=(Get-ChildItem $_.FullName).Length }'
    assert _judge(cmd)[0] == "ask"                       # 默认不放行脚本块
    assert _judge(cmd, allow=["get-childitem*for-each*"])[0] == "ask"   # 拼写不对也不放行
    assert _judge(cmd, allow=["get-childitem*foreach-object*"])[0] == "allow"
    # 禁止清单优先
    assert _judge(cmd, allow=["get-childitem*"], deny=["*"])[0] == "ask"


def test_default_autonomy_is_whitelist():
    assert A.normalize_shell_autonomy("") == "whitelist"
    assert A.normalize_shell_autonomy("nope") == "whitelist"
    assert A.normalize_shell_autonomy("AUTO") == "auto"
