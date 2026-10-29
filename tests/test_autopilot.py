# -*- coding: utf-8 -*-
"""自动化：目标状态机的纯函数部分（不调模型、不碰文件）"""
import threading

from app import autopilot as ap


def test_norm_criteria_accepts_strings_and_dicts():
    out = ap._norm_criteria([
        "pytest 通过",
        {"text": "没有语法错误", "cmd": "node --check web/app.js"},
        {"text": "", "cmd": ""},
    ])
    assert [c["text"] for c in out] == ["pytest 通过", "没有语法错误"]
    assert out[0]["source"] == "user"
    assert out[1]["cmd"] == "node --check web/app.js"


def test_norm_criteria_ignores_bad_input():
    assert ap._norm_criteria(None) == []
    assert ap._norm_criteria("pytest") == []
    assert ap._norm_criteria([1, 2]) == []


def test_json_loose_handles_fenced_and_noise():
    assert ap._json_loose('```json\n{"decision":"continue"}\n```')["decision"] == "continue"
    assert ap._json_loose('好的：{"decision":"done"} 以上')["decision"] == "done"
    assert ap._json_loose("没有 JSON") == {}
    assert ap._json_loose("") == {}


def test_goal_brief_injects_constraints_and_criteria():
    g = {
        "objective": "把首页改好看",
        "constraints": ["不要动 NOTE.md"],
        "criteria": [{"text": "构建通过", "cmd": "npm run build", "source": "boss"}],
        "plan": ["先看现状", "再改样式"],
    }
    text = ap._goal_brief(g)
    assert "把首页改好看" in text
    assert "不要动 NOTE.md" in text
    assert "npm run build" in text
    assert "先看现状" in text
    # 必须明确告诉员工「验收不是你说了算」
    assert "不是你自己宣布" in text


def test_all_pass_requires_every_item_true():
    # 空验收 = 不算完成（否则老板一说收工就结束了）
    assert ap._all_pass([]) is False
    assert ap._all_pass([{"ok": True}, {"ok": True}]) is True
    assert ap._all_pass([{"ok": True}, {"ok": False}]) is False
    # 不可自动验收（None）不能当成通过
    assert ap._all_pass([{"ok": True}, {"ok": None}]) is False


def test_summarize_collects_evidence_not_prose():
    events = [
        {"type": "delta", "text": "先改 "},
        {"type": "delta", "text": "样式"},
        {"type": "tool_done", "name": "write_file", "ok": True, "write": True},
        {"type": "tool_done", "name": "run_command", "ok": False, "write": False},
        {"type": "warn", "message": "轮次用尽"},
        {"type": "timing", "timing": {"rounds": 3, "finish_reason": "stop"}},
        {"type": "usage", "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
    ]
    out = ap._summarize(events)
    assert out["text"] == "先改 样式"
    assert out["tools"] == 2
    assert out["fails"] == 1
    assert out["writes"] == 1
    assert out["warns"] == ["轮次用尽"]
    assert out["timing"]["rounds"] == 3
    assert out["usage"]["prompt_tokens"] == 100


def test_summarize_replace_overrides_stream():
    # 裸调用被清理时会发 replace，之前累积的正文要整体替换
    out = ap._summarize([
        {"type": "delta", "text": "错的"},
        {"type": "replace", "text": "对的"},
        {"type": "delta", "text": "内容"},
    ])
    assert out["text"] == "对的内容"


def test_budget_over_by_turns_and_minutes():
    assert ap._budget_over({"turn": 29, "max_turns": 30}) == ""
    assert ap._budget_over({"turn": 30, "max_turns": 30}) == "回合数用尽"
    now = ap._now()
    g = {"turn": 0, "max_turns": 30, "max_minutes": 90, "started_at": now - 91 * 60}
    assert ap._budget_over(g) == "时长用尽"


def test_fake_handler_parses_sse_frames():
    h = ap._FakeHandler({"a": 1})
    assert h._body() == {"a": 1}
    h.wfile.write(b'data: {"type":"delta","text":"hi"}\n\n')
    h.wfile.write(b'data: {"type":"done"}\n\ndata: {"type":"timing"}\n\n')
    assert [e["type"] for e in h.events] == ["delta", "done", "timing"]
    assert h.events[0]["text"] == "hi"


def test_fake_handler_json_records_error():
    h = ap._FakeHandler({})
    h._json({"ok": False, "error": "busy"}, 409)
    assert h.status == 409
    assert h.payload["error"] == "busy"


def test_create_rejects_missing_inputs():
    # 只测「校验分支」：这三条都不会落盘、不会起线程
    assert ap.create("", "把首页改好看")["error"] == "缺少 session_id"
    assert ap.create("s_x", "")["error"].startswith("目标为空")
    assert ap.create("s_not_exist", "把首页改好看")["error"] == "会话不存在"


def test_int_or_falls_back():
    assert ap._int_or(5, 20, 30) == 5
    assert ap._int_or(None, 20, 30) == 20
    assert ap._int_or("x", None, 30) == 30
    assert ap._int_or(0, 0, 30) == 30


def test_norm_definition_splits_deliverables_checks_rubric():
    d = ap._norm_definition({
        "deliverables": ["web/index.html", {"name": "可执行", "path": "run.py"}],
        "checks": [
            {"text": "测试通过", "cmd": "pytest -q"},
            {"text": "入口存在", "path": "web/index.html", "kind": "file"},
            {"text": "服务可访问", "url": "http://localhost:8000", "kind": "http"},
            "无法机检：手感像原版",
        ],
        "rubric": [{"text": "操作手感接近原版", "evidence": "录屏 / 截图"}],
    })
    assert d["deliverables"][0]["path"] == "web/index.html"
    assert [c["kind"] for c in d["checks"]] == ["command", "file", "http"]
    assert len(d["rubric"]) == 2


def test_norm_definition_falls_back_to_overall_rubric():
    d = ap._norm_definition({})
    assert len(d["rubric"]) == 1 and d["rubric"][0]["id"] == "r0"


def test_definition_from_criteria_splits_checks_and_rubric():
    d = ap._definition_from_criteria([
        {"text": "构建通过", "cmd": "npm run build"},
        {"text": "看起来像原版", "cmd": ""},
    ])
    assert len(d["checks"]) == 1 and len(d["rubric"]) == 1


def test_machine_verdict_checks_files_and_deliverables(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    g = {"definition": {
        "deliverables": [{"id": "d1", "name": "a", "path": "a.txt"}],
        "checks": [
            {"id": "c1", "text": "入口存在", "kind": "file", "path": "a.txt"},
            {"id": "c2", "text": "缺文件", "kind": "file", "path": "missing.txt"},
        ],
        "rubric": [],
    }}
    v = ap._machine_verdict(g, str(tmp_path))
    assert v[0]["ok"] is True and v[1]["ok"] is True and v[2]["ok"] is False


def test_verify_definition_uses_verifier_and_never_guesses(tmp_path, monkeypatch):
    g = {"objective": "做一个东西",
         "definition": {"deliverables": [], "checks": [],
                        "rubric": [{"id": "r1", "text": "像原版", "evidence": "截图"}]}}
    monkeypatch.setattr(ap, "_sandbox", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(ap, "_ask_verifier",
                        lambda *a, **k: {"r1": {"ok": True, "out": "截图足够"}})
    v = ap._verify_definition(g, {"autopilot_verify": True})
    assert ap._all_pass(v) is True

    # 评审员没给结论：不能算通过，也不能猜
    monkeypatch.setattr(ap, "_ask_verifier", lambda *a, **k: {})
    v = ap._verify_definition(g, {"autopilot_verify": True})
    assert ap._all_pass(v) is False and v[0]["ok"] is None


def test_machine_verdict_skips_dangerous_command(tmp_path):
    g = {"definition": {"deliverables": [], "rubric": [],
                        "checks": [{"id": "c1", "text": "删库",
                                    "kind": "command", "cmd": "rm -rf /"}]}}
    v = ap._machine_verdict(g, str(tmp_path))
    assert v[0]["ok"] is None and "破坏性" in v[0]["out"]


def test_review_definition_adds_missing_rubric(monkeypatch):
    monkeypatch.setattr(ap, "_quick", lambda *a, **k: (
        '{"rubric":[{"text":"性能达标","evidence":"bench 输出"}]}'))

    d = {"deliverables": [], "checks": [],
         "rubric": [{"id": "r1", "text": "能玩", "evidence": "截图"}]}
    out = ap._review_definition({"objective": "做个游戏"}, d)
    assert len(out["rubric"]) == 2
    assert any(r["text"] == "性能达标" for r in out["rubric"])


def test_error_kind_and_retry_delay():
    assert ap._error_kind("连接超时") == "transient"
    assert ap._error_kind("502 bad gateway") == "transient"
    assert ap._error_kind("401 invalid api key") == "account"
    assert ap._error_kind("余额不足") == "account"
    assert ap._error_kind("400 bad request") == "fatal"
    assert ap._retry_delay(1) == 5
    assert ap._retry_delay(10) == 60


def test_progress_signature_and_no_progress():
    g = {"ledger": [{"writes": 1}], "plan": ["a"],
         "verdict": [{"id": "c1", "ok": False}]}
    sig = ap._progress_signature(g, {})
    assert ap._next_no_progress({"progress_sig": "", "no_progress": 0}, sig) == 0
    assert ap._next_no_progress({"progress_sig": sig, "no_progress": 1}, sig) == 2
    # 只改计划不算进展（防止反复 replan 骗过看门狗）；写入数变化才算
    assert ap._progress_signature(dict(g, plan=["a", "b"]), {}) == sig
    assert ap._progress_signature(dict(g, ledger=[{"writes": 2}]), {}) != sig


def test_goal_brief_includes_definition_checkpoint_and_policy():
    g = {"objective": "做一个小游戏", "question_policy": "smart",
         "definition": {
             "deliverables": [{"name": "入口", "path": "index.html"}],
             "checks": [{"text": "构建", "cmd": "npm run build", "kind": "command"}],
             "rubric": [{"text": "手感接近", "evidence": "录屏"}],
         },
         "checkpoint": {"turn": 3, "files": ["a.js"],
                        "verdict": {"ok": 1, "total": 2}},
         "plan": ["先跑起来"]}
    text = ap._goal_brief(g)
    assert "完成定义" in text and "npm run build" in text
    assert "断点" in text and "a.js" in text
    assert "提问策略：smart" in text


def test_autopilot_end_reason_strips_manual_continue():
    from app.api.chat import _autopilot_end_reason

    r = _autopilot_end_reason({
        "kind": "idle",
        "text": "计划里还有 2 步没完成。点「继续输出」接着做。",
        "action": "continue",
    })
    assert "点「继续输出」" not in r["text"]
    assert r["action"] == "" and r["autopilot"] is True
    stopped = {"kind": "stopped", "text": "你手动停止了本轮", "action": "continue"}
    assert _autopilot_end_reason(stopped) == stopped

    billing = {"kind": "billing",
               "text": "API 余额不足，请充值或切换服务商后点「重试」。",
               "action": "retry"}
    b = _autopilot_end_reason(billing)
    assert b["action"] == "" and "自动化" in b["text"]
    assert "点「重试」" not in b["text"]


def _goal(tmp_path, **over):
    now = ap._now()
    g = {
        "id": "g_test", "session_id": "s_test", "objective": "做一个网页小游戏",
        "sandbox": str(tmp_path),
        "state": "running", "pause_reason": "", "criteria": [], "definition": {},
        "plan": [], "turn": 0, "max_turns": 30, "max_minutes": 90,
        "use_budget": False, "watchdog_turns": 2, "stall_minutes": 0,
        "retry_max": 0, "verify": True, "question_policy": "smart",
        "pending": "", "ledger": [], "verdict": [], "decisions": [],
        "open_questions": [], "checkpoint": {}, "no_progress": 0,
        "progress_sig": "", "unknown_streak": 0, "bootstrap_tries": 0,
        "bootstrapped": False, "retries": 0, "last_reason": "", "last_error": "",
        "last_text": "", "tokens": {"prompt": 0, "completion": 0}, "schema": 2,
        "created_at": now, "updated_at": now, "started_at": now,
        "last_progress_at": now, "finished_at": 0,
    }
    g.update(over)
    return g


def _patch_loop(monkeypatch, tmp_path, quick, run_turn):
    monkeypatch.setattr(ap, "GOALS_FILE", str(tmp_path / "goals.json"))
    monkeypatch.setattr(ap, "_publish", lambda g: None)
    monkeypatch.setattr(ap, "_sandbox", lambda g: str(tmp_path))
    monkeypatch.setattr(ap, "_quick", quick)
    monkeypatch.setattr(ap, "_run_turn", run_turn)


def test_loop_bootstraps_executes_verifies_and_done(tmp_path, monkeypatch):
    import json as _json

    def quick(prompt, max_tokens, source):
        if "验收设计者" in prompt:
            return _json.dumps({
                "deliverables": [{"name": "入口", "path": "index.html"}],
                "checks": [],
                "rubric": [{"text": "能玩", "evidence": "截图"}],
                "plan": ["做一个 index.html"],
            }, ensure_ascii=False)
        if "验收审查员" in prompt:
            return "{}"
        if "独立验收员" in prompt:
            return _json.dumps(
                {"items": [{"id": "r1", "ok": True, "why": "证据足够"}]},
                ensure_ascii=False)
        return ""

    def turn(g, instruction, ev):
        (tmp_path / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        return {"ok": True, "text": "done", "tools": 1, "fails": 0, "writes": 1,
                "warns": [], "timing": {}, "usage": {}, "end_reason": {},
                "files": ["index.html"], "errors": []}

    _patch_loop(monkeypatch, tmp_path, quick, turn)
    ap.save_goals({"items": [_goal(tmp_path)]})
    ap._loop("g_test", threading.Event())

    out = ap.get_goal("g_test")
    assert out["state"] == "done"
    assert out["turn"] == 1
    assert out["definition"]["deliverables"][0]["path"] == "index.html"
    assert out["verdict"] and all(v["ok"] is True for v in out["verdict"])


def test_loop_pauses_after_repeated_no_progress(tmp_path, monkeypatch):
    import json as _json

    def quick(prompt, max_tokens, source):
        if "验收设计者" in prompt:
            return _json.dumps({
                "deliverables": [],
                "checks": [],
                "rubric": [{"text": "能玩", "evidence": "截图"}],
                "plan": ["做一个 index.html"],
            }, ensure_ascii=False)
        if "验收审查员" in prompt:
            return "{}"
        if "独立验收员" in prompt:
            return _json.dumps(
                {"items": [{"id": "r1", "ok": False, "why": "没有证据"}]},
                ensure_ascii=False)
        return ""

    def turn(g, instruction, ev):
        return {"ok": True, "text": "nothing", "tools": 0, "fails": 0,
                "writes": 0, "warns": [], "timing": {}, "usage": {},
                "end_reason": {}, "files": [], "errors": []}

    _patch_loop(monkeypatch, tmp_path, quick, turn)
    monkeypatch.setattr(ap, "_boss_ask", lambda g, v, t: {
        "decision": "continue", "instruction": "next",
        "plan_patch": [], "reason": "",
    })
    ap.save_goals({"items": [_goal(tmp_path, watchdog_turns=2)]})
    ap._loop("g_test", threading.Event())

    out = ap.get_goal("g_test")
    assert out["state"] == "needs_human"
    assert out["pause_reason"] == "stuck"
    assert out["turn"] == 5


def test_recover_resumes_running_goal(tmp_path, monkeypatch):
    started = []
    monkeypatch.delenv("GALGAME_NO_AUTORESUME", raising=False)
    monkeypatch.setattr(ap, "GOALS_FILE", str(tmp_path / "goals.json"))
    monkeypatch.setattr(ap, "_publish", lambda g: None)
    monkeypatch.setattr(ap, "_capture_pending_tools", lambda g: {})
    monkeypatch.setattr(ap, "load_config",
                        lambda: {"autopilot_resume_on_start": True})
    monkeypatch.setattr(ap, "start", lambda gid: started.append(gid))
    ap.save_goals({"items": [_goal(tmp_path, state="running")]})

    assert ap.recover() == 1
    out = ap.get_goal("g_test")
    assert out["state"] == "running"
    assert out["last_reason"].startswith("应用重启后自动恢复")
    assert started == ["g_test"]


def test_goal_active_rules():
    assert ap._goal_active({"state": "running"}) is True
    assert ap._goal_active({"state": "retrying"}) is True
    assert ap._goal_active({"state": "needs_human"}) is True
    # 旧版 paused 没有 pause_reason：历史遗留，不能当成当前目标
    assert ap._goal_active({"state": "paused", "pause_reason": ""}) is False
    assert ap._goal_active({"state": "paused", "pause_reason": "user"}) is True
    assert ap._goal_active({"state": "done"}) is False
    assert ap._goal_active({"state": "stopped"}) is False
    assert ap._goal_active({"state": "failed", "pause_reason": ""}) is False
    assert ap._goal_active({"state": "failed", "pause_reason": "error"}) is True
    # 已经落盘过 active 的旧目标以字段为准
    assert ap._goal_active({"state": "paused", "active": True}) is True
    # 从没跑过（turn=0）且创建很久的暂停目标：按历史处理
    assert ap._goal_active({
        "state": "paused", "pause_reason": "user", "turn": 0,
        "created_at": ap._now() - 7 * 3600}) is False
    assert ap._goal_active({
        "state": "paused", "pause_reason": "user", "turn": 0,
        "created_at": ap._now() - 3600}) is True
    assert ap._goal_active({
        "state": "paused", "pause_reason": "user", "turn": 3,
        "created_at": ap._now() - 7 * 3600}) is True


def test_migrate_goal_marks_historical_pause_inactive():
    g = {"state": "paused", "pause_reason": "", "criteria": []}
    ap._migrate_goal(g)
    assert g["active"] is False

    g2 = {"state": "paused", "pause_reason": "user", "criteria": []}
    ap._migrate_goal(g2)
    assert g2["active"] is True

    g3 = {"state": "needs_human", "pause_reason": "", "criteria": []}
    ap._migrate_goal(g3)
    assert g3["active"] is True


def test_goal_for_session_ignores_historical_paused(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "GOALS_FILE", str(tmp_path / "goals.json"))
    old = _goal(tmp_path, id="g_old", state="paused", pause_reason="", active=False)
    new = _goal(tmp_path, id="g_new", state="running", active=True,
                created_at=ap._now() + 10)
    ap.save_goals({"items": [old, new]})
    hit = ap.goal_for_session("s_test")
    assert hit and hit["id"] == "g_new"

    ap.save_goals({"items": [old]})
    assert ap.goal_for_session("s_test") is None


