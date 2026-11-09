# -*- coding: utf-8 -*-
"""验收子代理：结构化解析、证据强制、客观封顶与合并判定。"""
import json

from app import quality_gate as qg


def _report(**kw):
    base = {"verdict": "pass", "score": 90,
            "criteria": [{"criterion": "文件存在", "passed": True,
                          "evidence": "app/x.py:1"}],
            "gaps": [], "next_action": "无"}
    base.update(kw)
    return base


def test_parse_report_reads_fenced_json():
    text = ("说明\n```json\n" + json.dumps(_report(), ensure_ascii=False) + "\n```\n")
    rep = qg.parse_report(text)
    assert rep and rep["verdict"] == "pass" and rep["score"] == 90
    assert rep["criteria"][0]["passed"] is True


def test_parse_report_requires_evidence_for_pass():
    rep = qg.parse_report(json.dumps(_report(criteria=[
        {"criterion": "测试通过", "passed": True, "evidence": ""}])))
    assert rep["criteria"][0]["passed"] is False
    assert "证据" in rep["criteria"][0]["note"]


def test_parse_report_bad_shape():
    assert qg.parse_report("不是 JSON") is None
    rep = qg.parse_report(json.dumps({"verdict": "??", "score": "x"}))
    assert rep["verdict"] == "fail" and rep["score"] == 0


def test_unresolved_failures_only_when_not_retried():
    log = [
        {"tool": "run_command", "args": {"argv": ["pytest"]}, "ok": False, "result": "FAIL"},
        {"tool": "run_command", "args": {"argv": ["pytest"]}, "ok": True, "result": "ok"},
        {"tool": "read_file", "args": {"path": "a.py"}, "ok": False, "result": "not found"},
    ]
    bad = qg.unresolved_failures(log)
    assert len(bad) == 1 and bad[0]["tool"] == "read_file"


def test_score_cap_and_forced_fail():
    facts = qg.build_facts(
        tool_log=[{"tool": "write_file", "args": {"path": "a.py"}, "ok": False}],
        plan={"steps": [{"id": "S1", "status": "pending"}]},
        missing_files=["a.py"], metric=50, metric_min=70)
    cap, reasons = qg.score_cap(facts, 70)
    assert cap == 40 and len(reasons) >= 3
    merged = qg.merge_report(_report(score=95), facts, 70)
    assert merged["verdict"] == "fail" and merged["score"] == 40
    assert merged["gaps"]


def test_merge_report_pass_below_min_fails():
    facts = qg.build_facts()
    merged = qg.merge_report(_report(score=60), facts, 70)
    assert merged["verdict"] == "fail" and merged["score"] == 60


def test_merge_report_keeps_pass_when_clean():
    facts = qg.build_facts()
    merged = qg.merge_report(_report(score=85), facts, 70)
    assert merged["verdict"] == "pass" and merged["score"] == 85


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
    txt = qg.review_text({"verdict": "fail", "score": 55, "min_score": 70,
                          "criteria": [{"criterion": "页面能打开", "passed": False,
                                        "evidence": ""}],
                          "gaps": ["没有截图"], "next_action": "补截图"})
    assert "未通过" in txt and "55 分" in txt and "没有截图" in txt


def test_plan_checks_collects_done_when_and_verify():
    plan = {"done_when": ["首页能打开"],
            "milestones": [{"id": "M1", "done_when": "接口返回 200"}],
            "steps": [{"id": "S1", "verify": "跑 pytest 全绿"},
                      {"id": "S2", "verify": "跑 pytest 全绿"}]}
    checks = qg.plan_checks(plan)
    assert checks == ["首页能打开", "接口返回 200", "跑 pytest 全绿"]


def test_missing_plan_checks_caps_score():
    plan = {"done_when": ["首页能打开"],
            "steps": [{"id": "S1", "status": "done", "verify": "跑 pytest 全绿"}]}
    facts = qg.build_facts(plan=plan)
    covered = _report(criteria=[
        {"criterion": "首页能打开", "passed": True, "evidence": "index.html:1"},
        {"criterion": "跑 pytest 全绿", "passed": True, "evidence": "$ pytest → 12 passed"},
    ])
    ok = qg.merge_report(covered, facts, 70)
    assert ok["verdict"] == "pass" and ok["score"] == 90

    partial = _report(criteria=[
        {"criterion": "首页能打开", "passed": True, "evidence": "index.html:1"}])
    bad = qg.merge_report(partial, facts, 70)
    assert bad["verdict"] == "fail" and bad["score"] == 65
    assert any("没有核对计划里的验收条件" in g for g in bad["gaps"])
