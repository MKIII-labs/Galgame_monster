# -*- coding: utf-8 -*-
"""Shell 三档：strict 不动、standard 放管道、trusted 放串联；每段仍过白名单。"""
from app import agent


def test_strict_rejects_pipe(tmp_path):
    plan, err = agent._cmd_plan({"cmd": "git status | git log"}, str(tmp_path), "strict")
    assert plan is None
    assert "不支持" in err


def test_standard_allows_pipe(tmp_path):
    plan, err = agent._cmd_plan({"cmd": "git status | git log"}, str(tmp_path), "standard")
    assert err == "" and plan["shell"] is True
    assert len(plan["segments"]) == 2


def test_standard_rejects_non_whitelisted_segment(tmp_path):
    plan, err = agent._cmd_plan({"cmd": "git status | powershell -c x"},
                                str(tmp_path), "standard")
    assert plan is None and "第 2 段" in err


def test_standard_rejects_redirect_outside(tmp_path):
    plan, err = agent._cmd_plan({"cmd": "git status > ../x.txt"}, str(tmp_path), "standard")
    assert plan is None and "沙箱" in err


def test_standard_allows_redirect_inside(tmp_path):
    plan, err = agent._cmd_plan({"cmd": "git status > out.txt"}, str(tmp_path), "standard")
    assert err == ""
    assert plan["segments"][0]["redirect"] == ">"
    assert plan["segments"][0]["target"].endswith("out.txt")


def test_standard_rejects_semicolon(tmp_path):
    plan, err = agent._cmd_plan({"cmd": "git status ; git log"}, str(tmp_path), "standard")
    assert plan is None and ";" in err


def test_trusted_allows_semicolon(tmp_path):
    plan, err = agent._cmd_plan({"cmd": "git status ; git log"}, str(tmp_path), "trusted")
    assert err == "" and len(plan["segments"]) == 2


def test_trusted_still_checks_each_command(tmp_path):
    plan, err = agent._cmd_plan({"cmd": "git status ; powershell -c x"},
                                str(tmp_path), "trusted")
    assert plan is None and "禁止执行" in err


def test_background_amp_rejected_in_every_mode(tmp_path):
    for mode in ("standard", "trusted"):
        plan, err = agent._cmd_plan({"cmd": "git status & git log"}, str(tmp_path), mode)
        assert plan is None and "&" in err


def test_argv_list_is_untouched(tmp_path):
    plan, err = agent._cmd_plan({"argv": ["git", "status"]}, str(tmp_path), "trusted")
    assert err == "" and plan["shell"] is False
