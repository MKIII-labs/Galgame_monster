# -*- coding: utf-8 -*-
"""验收子代理：结构化解析、证据强制、客观硬伤与合并判定（无评分）。"""
import json

from app import quality_gate as qg


def _report(**kw):
    base = {"verdict": "pass",
            "criteria": [{"criterion": "文件存在", "passed": True,
                          "evidence": "app/x.py:1"}],
            "gaps": [], "next_action": "无"}
    base.update(kw)
    return base


def test_parse_report_reads_fenced_json():
    text = ("说明\n```json\n" + json.dumps(_report(), ensure_ascii=False) + "\n```\n")
    rep = qg.parse_report(text)
    assert rep and rep["verdict"] == "pass"
    assert rep["criteria"][0]["passed"] is True
    assert "score" not in rep


def test_parse_report_requires_evidence_for_pass():
    rep = qg.parse_report(json.dumps(_report(criteria=[
        {"criterion": "测试通过", "passed": True, "evidence": ""}])))
    assert rep["criteria"][0]["passed"] is False
    assert "证据" in rep["criteria"][0]["note"]


def test_parse_report_bad_shape():
    assert qg.parse_report("不是 JSON") is None
    rep = qg.parse_report(json.dumps({"verdict": "??"}))
    assert rep["verdict"] == "fail"
    assert "score" not in rep


def test_unresolved_failures_only_when_not_retried():
    log = [
        {"tool": "run_command", "args": {"argv": ["pytest"]}, "ok": False, "result": "FAIL"},
        {"tool": "run_command", "args": {"argv": ["pytest"]}, "ok": True, "result": "ok"},
        {"tool": "read_file", "args": {"path": "a.py"}, "ok": False, "result": "boom"},
    ]
    bad = qg.unresolved_failures(log)
    assert len(bad) == 1 and bad[0]["tool"] == "read_file"


def test_unresolved_failures_ignore_premise_errors():
    """前提不成立的失败（不是 git 仓库 / 本地文件不支持 http）不算未修复。"""
    log = [
        {"tool": "git_status", "args": {}, "ok": False,
         "result": "fatal: not a git repository (or any of the parent directories): .git"},
        {"tool": "open_preview", "args": {"url": "file:///x.html"}, "ok": False,
         "result": "❌ 只支持 http / https"},
    ]
    assert qg.unresolved_failures(log) == []


def test_hard_facts_force_fail():
    facts = qg.build_facts(
        tool_log=[{"tool": "write_file", "args": {"path": "a.py"}, "ok": False}],
        plan={"steps": [{"id": "S1", "status": "pending"}]},
        missing_files=["a.py"])
    merged = qg.merge_report(_report(), facts)
    assert merged["verdict"] == "fail" and merged["passed"] is False
    assert any("磁盘" in g for g in merged["gaps"])
    assert any("没有修复" in g for g in merged["gaps"])


def test_merge_report_keeps_pass_when_clean():
    facts = qg.build_facts()
    merged = qg.merge_report(_report(), facts)
    assert merged["verdict"] == "pass" and merged["passed"] is True


def test_failed_criterion_forces_fail():
    facts = qg.build_facts()
    merged = qg.merge_report(_report(criteria=[
        {"criterion": "页面能打开", "passed": False, "evidence": "打不开"}]),
        facts)
    assert merged["verdict"] == "fail" and merged["passed"] is False


def test_build_brief_marks_claims_as_untrusted():
    brief = qg.build_brief("做一个页面", "S1 写文件",
                           [{"tool": "write_file", "ok": True, "result": "done"}],
                           qg.build_facts(), summary="已经全部完成",
                           answer="完毕", sandbox="/tmp", has_tools=False)
    assert "不能当证据" in brief
    assert "只能依据" in brief


def _read_schema():
    return {"type": "function", "function": {
        "name": "read",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "file", "search"]}}}}}


def test_build_brief_lists_available_tools():
    """验收代理必须看到确切工具名；以前只写「读文件 / 跑命令」，模型会猜旧名。"""
    brief = qg.build_brief("任务", tools=[_read_schema()])
    assert "【可用工具（只能调用这些名字）】" in brief
    assert "- read（action: list / file / search）" in brief


def test_allowed_names_accepts_legacy_aliases():
    names = qg.allowed_names([_read_schema()])
    assert "read" in names
    assert "read_file" in names and "list_dir" in names and "search_files" in names
    # 没有下发的元工具组，旧名也不该被放行
    assert "run_command" not in names and "delegate" not in names


def test_run_accepts_legacy_tool_name():
    """模型沿用 read_file 时不应被判成「不在可用范围」，执行端会自己归一。"""
    report = _report()
    rounds = {"n": 0}

    def chat_fn(_ctx, _messages, _schemas, _stop):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return {"visible": "", "usage": {},
                    "tool_calls": [{"tool": "read_file", "args": {"path": "a.py"}}]}
        return {"visible": json.dumps(report, ensure_ascii=False), "usage": {},
                "tool_calls": []}

    called = []

    def tool_fn(name, args):
        called.append((name, args))
        return True, "文件内容"

    res = qg.run("任务", {"chat_fn": chat_fn, "schemas": [_read_schema()],
                          "tool_fn": tool_fn, "max_rounds": 4})
    assert called == [("read_file", {"path": "a.py"})]
    assert res["tools"] == ["read_file"]
    assert res["ok"] is True


def test_review_text_lists_gaps():
    txt = qg.review_text({"verdict": "fail",
                          "criteria": [{"criterion": "页面能打开", "passed": False,
                                        "evidence": ""}],
                          "gaps": ["没有截图"], "next_action": "补截图"})
    assert "未通过" in txt and "0/1 项通过" in txt and "没有截图" in txt


def test_plan_checks_only_collects_goal_level_conditions():
    """步骤 verify 只是核对方式，不能变成硬性验收项（否则会逼模型重试注定失败的命令）。"""
    plan = {"done_when": ["首页能打开"],
            "milestones": [{"id": "M1", "done_when": "接口返回 200"}],
            "steps": [{"id": "S1", "verify": "跑 pytest 全绿"},
                      {"id": "S2", "verify": "跑 pytest 全绿"}]}
    assert [x["criterion"] for x in qg.plan_checks(plan)] == ["首页能打开", "接口返回 200"]
    assert all(x["source"] == "plan" and x["severity"] == "blocking"
               for x in qg.plan_checks(plan))
    assert qg.plan_hints(plan) == ["跑 pytest 全绿"]


def test_missing_plan_checks_fails():
    plan = {"done_when": ["首页能打开", "接口返回 200"],
            "steps": [{"id": "S1", "status": "done", "verify": "跑 pytest 全绿"}]}
    facts = qg.build_facts(plan=plan)
    covered = _report(criteria=[
        {"criterion": "首页能打开", "passed": True, "evidence": "index.html:1"},
        {"criterion": "接口返回 200", "passed": True, "evidence": "$ curl → 200"},
        {"criterion": "跑 pytest 全绿", "passed": True, "evidence": "$ pytest → 12 passed"},
    ])
    ok = qg.merge_report(covered, facts)
    assert ok["verdict"] == "pass" and ok["passed"] is True

    partial = _report(criteria=[
        {"criterion": "首页能打开", "passed": True, "evidence": "index.html:1"}])
    bad = qg.merge_report(partial, facts)
    assert bad["verdict"] == "fail" and bad["passed"] is False
    assert any("没有核对计划里的验收条件" in g for g in bad["gaps"])


def test_pass_drops_gaps_and_next_action():
    """通过后不再输出差距 / 下一步，避免主代理为不存在的差距反复返工。"""
    merged = qg.merge_report(
        _report(gaps=["建议补一条现状说明"], next_action="去补说明"),
        qg.build_facts())
    assert merged["passed"] is True and merged["gaps"] == []
    assert merged["next_action"] == ""
    assert merged["suggestions"] == ["建议补一条现状说明"]
    assert "建议补一条现状说明" not in qg.review_text(merged)


def test_blocked_is_terminal():
    """环境无法验证时标记为终止，主循环据此收尾而不是继续返工。"""
    merged = qg.merge_report(
        _report(verdict="blocked", criteria=[], gaps=["缺少参考图"],
                next_action="等用户提供参考图"),
        qg.build_facts())
    assert merged["passed"] is False and merged["terminal"] is True
    assert merged["verdict"] == "blocked"


def test_fail_overrides_pass_wording_in_next_action():
    """硬伤判未通过时，不能保留验收模型写的「验收通过」下一步文案。"""
    facts = qg.build_facts(tool_log=[
        {"tool": "computer_act", "ok": False, "result": "❌ 不支持的按键：enter"}])
    merged = qg.merge_report(
        _report(next_action="本轮验收通过，无需返工；如需继续请另起任务"), facts)
    assert merged["passed"] is False
    assert "验收通过" not in merged["next_action"]
    assert "先修复" in merged["next_action"]
    assert "验收通过" not in qg.review_text(merged)


def test_empty_report_cannot_pass():
    """没有验收项的报告不能因为「没有 blocking 失败项」被判过。"""
    merged = qg.merge_report({"verdict": "fail", "criteria": [], "gaps": []},
                             qg.build_facts())
    assert merged["passed"] is False and merged["verdict"] == "fail"


def test_parse_report_marks_sources_and_locks_user_items():
    rep = qg.parse_report(json.dumps(_report(criteria=[
        {"criterion": "用户要求", "passed": False,
         "source": "user", "severity": "advisory"}])))
    c = rep["criteria"][0]
    assert c["source"] == "user" and c["severity"] == "blocking" and c["locked"] is True


def test_advisory_failure_does_not_block():
    """验收代理自己补的检查默认 advisory：未通过只给建议，不否决交付。"""
    merged = qg.merge_report(_report(criteria=[
        {"criterion": "用户要求：页面能打开", "passed": True, "evidence": "index.html:1"},
        {"criterion": "建议补移动端适配", "passed": False,
         "source": "reviewer", "severity": "advisory"}]), qg.build_facts())
    assert merged["passed"] is True and merged["verdict"] == "pass"
    assert any("移动端" in s for s in merged["suggestions"])


def test_plan_check_cannot_be_downgraded_by_reviewer():
    """计划条件被验收代理标成 advisory 也要强制回 blocking（不能靠改 source 绕过）。"""
    facts = qg.build_facts(plan={"done_when": ["首页能打开"]})
    merged = qg.merge_report(_report(criteria=[
        {"criterion": "首页能打开", "passed": False,
         "source": "reviewer", "severity": "advisory"}]), facts)
    assert merged["passed"] is False
    assert merged["criteria"][0]["severity"] == "blocking"


def test_accepted_challenge_downgrades_criterion():
    facts = qg.build_facts(plan={"done_when": ["接口返回 200"]})
    merged = qg.merge_report(_report(
        verdict="fail",
        criteria=[{"criterion": "接口返回 200", "passed": False,
                   "evidence": "沙箱起不了服务"}],
        challenge_results=[{"criterion": "接口返回 200", "accepted": True,
                            "reason": "环境确实无法起服务"}]), facts)
    assert merged["passed"] is True and merged["verdict"] == "pass"
    assert merged["criteria"][0]["severity"] == "advisory"


def test_rejected_challenge_stops_rework():
    """申诉被驳回后不再对同一项返工：本轮终止，交用户裁决。"""
    facts = qg.build_facts(plan={"done_when": ["接口返回 200"]})
    merged = qg.merge_report(_report(
        verdict="fail",
        criteria=[{"criterion": "接口返回 200", "passed": False,
                   "evidence": "$ curl → refused"}],
        challenge_results=[{"criterion": "接口返回 200", "accepted": False,
                            "reason": "可以用本地 stub 验证"}]), facts)
    assert merged["passed"] is False
    assert merged["arbitrate"] is True and merged["terminal"] is True
    assert "申诉未被采纳" in qg.review_text(merged)


def test_accepted_challenge_excuses_missing_plan_check():
    """申诉通过后验收代理直接把该项从清单删掉，也不能算「漏核对计划条件」。"""
    facts = qg.build_facts(plan={"done_when": ["接口返回 200"]})
    merged = qg.merge_report(_report(
        verdict="fail",
        criteria=[{"criterion": "首页能打开", "passed": True,
                   "evidence": "index.html:1"}],
        challenge_results=[{"criterion": "接口返回 200", "accepted": True,
                            "reason": "环境无法起服务"}]), facts)
    assert merged["passed"] is True and merged["verdict"] == "pass"


def test_unanswered_challenge_counts_as_rejected():
    """验收代理漏写 challenge_results：按未接受处理，停止同一项返工。"""
    facts = qg.build_facts(plan={"done_when": ["接口返回 200"]})
    facts["challenges"] = ["接口返回 200"]
    merged = qg.merge_report(_report(verdict="fail", criteria=[
        {"criterion": "接口返回 200", "passed": False,
         "evidence": "$ curl → refused"}]), facts)
    assert merged["passed"] is False
    assert merged["arbitrate"] is True and merged["terminal"] is True


def test_normalize_challenges_rejects_locked_and_dupes():
    crit = [{"criterion": "用户要求：保存按钮可用",
             "source": "user", "severity": "blocking"},
            {"criterion": "接口返回 200", "source": "plan", "severity": "blocking"}]
    ok, errs = qg.normalize_challenges(
        [{"criterion": "接口返回 200", "type": "env_impossible",
          "evidence": "本地没有服务"}], crit)
    assert len(ok) == 1 and not errs
    bad, berr = qg.normalize_challenges(
        [{"criterion": "用户要求：保存按钮可用", "type": "plan_drift",
          "evidence": "用户没提"}], crit)
    assert bad == [] and any("用户明确要求" in e for e in berr)
    used = {ok[0]["key"]: 1}
    again, aerr = qg.normalize_challenges(
        [{"criterion": "接口返回 200", "type": "env_impossible",
          "evidence": "本地没有服务"}], crit, used)
    assert again == [] and any("已经申诉过" in e for e in aerr)


def test_build_brief_includes_challenges():
    brief = qg.build_brief("任务", challenges=[
        {"criterion": "接口返回 200", "type": "env_impossible",
         "evidence": "本地无服务"}])
    assert "【主模型的申诉" in brief and "challenge_results" in brief
