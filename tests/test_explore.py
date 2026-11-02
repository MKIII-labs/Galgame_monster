# -*- coding: utf-8 -*-
"""并行探索子代理：参数收敛、预算、共享缓存、输出压缩、证据/验收/写入。"""
import threading
import time

import pytest

from app import agent, explore


@pytest.fixture(autouse=True)
def _no_evidence(monkeypatch, tmp_path):
    """旧用例聚焦调度/预算；证据核验由 test_evidence_* 显式开启。"""
    monkeypatch.setattr(explore, "VERIFY_EVIDENCE", False)
    monkeypatch.setattr(explore, "DATA_DIR", str(tmp_path))   # 子会话落盘隔离


def test_normalize_args_limits_and_roles():
    bad, err = explore.normalize_args({})
    assert bad is None and "questions" in err

    args = {"goal": " 查清鉴权 ", "questions": [
        {"ask": "入口在哪", "scope": ["app/"], "hint_files": ["app/api/chat.py"]},
        "token 校验逻辑",
        {"ask": ""},
        "多余的第四个",
        "多余的第五个",
    ]}
    norm, err = explore.normalize_args(args)
    assert err == ""
    assert norm["goal"] == "查清鉴权"
    assert len(norm["questions"]) == 4          # 空问题被跳过
    assert norm["questions"][0]["role"] == "finder"
    assert norm["questions"][1]["role"] == "reader"
    assert norm["questions"][0]["hint_files"] == ["app/api/chat.py"]
    assert norm["questions"][0]["scope"] == ["app/"]

    many, _ = explore.normalize_args({"goal": "g", "questions": ["a", "b", "c", "d", "e"]})
    assert len(many["questions"]) == 5          # 最多 5 个


def test_normalize_args_write_and_acceptance():
    norm, err = explore.normalize_args({
        "goal": "g", "allow_write": True, "deliverable": "改动建议",
        "acceptance": ["cites>=2", "answers>=1"],
        "questions": [{"ask": "q1"}, "q2"]})
    assert err == "" and norm["allow_write"] is True
    assert norm["deliverable"] == "改动建议"
    assert norm["acceptance"] == ["cites>=2", "answers>=1"]
    assert [q["role"] for q in norm["questions"]] == ["worker", "worker"]


def test_budget_counts_and_exhausts():
    b = explore.Budget(100)
    assert not b.exhausted()
    b.spend(60)
    assert not b.exhausted()
    b.spend(40)
    assert b.exhausted() and b.used == 100
    assert explore.Budget(0).exhausted() is False


def test_read_cache_dedupes(monkeypatch, tmp_path):
    calls = []

    def fake_run_tool(sandbox, name, args, **kwargs):
        calls.append((name, args))
        return "内容", None, False

    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    cache = explore.ReadCache(str(tmp_path))
    assert cache.call("read_file", {"path": "a.py"}) == "内容"
    assert cache.call("read_file", {"path": "a.py"}) == "内容"
    assert len(calls) == 1                       # 同一调用只执行一次


def test_format_results_marks_failures_and_keeps_full_text():
    res = {"goal": "查鉴权", "tokens": 12, "ms": 1500,
           "results": [{"id": "q1", "role": "finder", "ok": True,
                        "text": "结论 app/x.py:10", "error": ""},
                       {"id": "q2", "role": "reader", "ok": False,
                        "text": "", "error": "超时"}]}
    text = explore.format_results(res)
    assert "查鉴权" in text and "app/x.py:10" in text and "超时" in text

    long_res = dict(res)
    long_res["results"] = [{"id": "q%d" % i, "role": "reader", "ok": True,
                            "text": "x" * 900, "error": ""} for i in range(3)]
    assert explore.format_results(long_res).count("x" * 900) == 3   # 不再截断


def test_run_loops_tools_and_returns_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "run_tool",
                        lambda s, n, a: ("源码内容", None, False))
    lock = threading.Lock()
    state = {"n": 0}

    def fake_chat(ctx, messages, schemas, stop_event):
        with lock:
            state["n"] += 1
            first = state["n"] == 1
        if first:
            return {"visible": "", "usage": {"total_tokens": 80},
                    "tool_calls": [{"tool": "search_code", "args": {"query": "鉴权"}}],
                    "native_fallback": False}
        return {"visible": "结论：入口在 app/api/chat.py:749", "usage": {"total_tokens": 40},
                "tool_calls": [], "native_fallback": False}

    marks = []
    res = explore.run(
        {"goal": "查鉴权", "questions": [{"ask": "入口在哪", "hint_files": ["app/api/chat.py"]}]},
        {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
         "emit": marks.append, "budget": 10000, "max_agents": 3, "chat_fn": fake_chat})
    assert res["ok"] is True
    assert res["results"][0]["text"].startswith("结论")
    assert res["results"][0]["rounds"] == 2 and res["results"][0]["tools"] == ["search_code"]
    assert res["tokens"] == 120
    kinds = [m["type"] for m in marks]
    assert kinds[0] == "subagent_start" and "subagent_step" in kinds
    assert kinds[-1] == "subagent_all"


def test_run_respects_max_agents(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "run_tool",
                        lambda s, n, a, **k: ("x", None, False))

    def fake_chat(ctx, messages, schemas, stop_event):
        return {"visible": "结论 app/a.py:1", "usage": {"total_tokens": 10},
                "tool_calls": [], "native_fallback": False}

    res = explore.run(
        {"goal": "g", "questions": [{"ask": "q1"}, {"ask": "q2"}, {"ask": "q3"}]},
        {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
         "budget": 0, "max_agents": 2, "chat_fn": fake_chat})
    assert len(res["results"]) == 2
    assert all(r["ok"] for r in res["results"])


# ============================================================
# 第 2 阶段：反证者与冲突复核
# ============================================================
def test_normalize_args_reads_verify():
    norm, _ = explore.normalize_args(
        {"goal": "g", "questions": ["q"], "verify": True})
    assert norm["verify"] is True
    norm2, _ = explore.normalize_args({"goal": "g", "questions": ["q"]})
    assert norm2["verify"] is False


def test_looks_conflicted():
    assert explore._looks_conflicted([{"text": "这两份结论互相矛盾"}]) is True
    assert explore._looks_conflicted([{"text": "两份结论一致"}]) is False
    assert explore._looks_conflicted([]) is False


def _chat_by_role(critic_text, scout_text="结论 app/x.py:10"):
    def fake_chat(ctx, messages, schemas, stop_event):
        system = str((messages[0] or {}).get("content") or "")
        text = critic_text if "反证者" in system else scout_text
        return {"visible": text, "usage": {"total_tokens": 30},
                "tool_calls": [], "native_fallback": False}
    return fake_chat


def test_verify_appends_critic_and_warns(tmp_path):
    res = explore.run(
        {"goal": "查鉴权", "questions": [{"ask": "入口在哪"}], "verify": True},
        {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
         "budget": 0, "chat_fn": _chat_by_role(
             "结论：反驳\n依据：app/x.py:12\n修正：入口在函数内\n冲突：无")})
    assert res["verify"] is True
    assert [r["role"] for r in res["results"]] == ["finder", "critic"]
    text = explore.format_results(res)
    assert text.startswith("⚠ 复核发现反驳或冲突")
    assert "[critic]" in text


def test_conflict_words_auto_trigger_critic(tmp_path):
    res = explore.run(
        {"goal": "查鉴权", "questions": [{"ask": "入口在哪"}]},   # 没传 verify
        {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
         "budget": 0, "chat_fn": _chat_by_role(
             "结论：支持\n依据：app/x.py:10\n修正：无\n冲突：无",
             scout_text="入口在 a，但两处实现说法不同，存在矛盾")})
    assert res["verify"] is True
    assert any(r["role"] == "critic" for r in res["results"])


def test_no_critic_without_verify_or_conflict(tmp_path):
    res = explore.run(
        {"goal": "查鉴权", "questions": [{"ask": "入口在哪"}]},
        {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
         "budget": 0, "chat_fn": _chat_by_role("结论：支持")})
    assert res["verify"] is False
    assert all(r["role"] != "critic" for r in res["results"])


# ============================================================
# 可信侦察队：证据核验 / 验收契约 / 写入 / 落盘 / 黑板
# ============================================================
def _ctx(tmp_path, **kw):
    base = {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x",
            "model": "m", "budget": 0}
    base.update(kw)
    return base


def _reply(text="", calls=None):
    return {"visible": text, "usage": {"total_tokens": 5},
            "tool_calls": calls or [], "native_fallback": False}


def test_evidence_retries_once_then_passes(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    replies = [_reply("结论：入口在 nope.py:99"), _reply("结论：入口在 a.py:1")]
    seen = []

    def fake_chat(ctx, messages, schemas, stop_event):
        seen.append(list(messages))
        return replies[min(len(seen) - 1, len(replies) - 1)]

    res = explore.run({"goal": "g", "questions": [{"ask": "入口"}]},
                      _ctx(tmp_path, verify_evidence=True, chat_fn=fake_chat))
    r = res["results"][0]
    assert len(seen) == 2                          # 引用缺失 → 重试一次
    assert r["ok"] is True and r["evidence"]["missing"] == []
    assert "nope.py:99" in seen[1][-1]["content"]  # 重试时把错误告诉它


def test_evidence_still_missing_marks_failed(tmp_path):
    seen = []

    def fake_chat(ctx, messages, schemas, stop_event):
        seen.append(1)
        return _reply("结论：入口在 nope.py:99")

    res = explore.run({"goal": "g", "questions": [{"ask": "入口"}]},
                      _ctx(tmp_path, verify_evidence=True, chat_fn=fake_chat))
    r = res["results"][0]
    assert len(seen) == 2
    assert r["ok"] is False and "引用无法核对" in r["error"]


def test_acceptance_passes_and_fails(tmp_path):
    def fake_chat(ctx, messages, schemas, stop_event):
        ask = str(messages[-1].get("content") or "")
        return _reply("结论：a.py:1" if "q1" in ask else "结论：b.py:2")

    ok = explore.run({"goal": "g", "questions": ["q1", "q2"],
                      "acceptance": ["cites>=2", "answers>=2"]},
                     _ctx(tmp_path, chat_fn=fake_chat))
    assert ok["acceptance"]["passed"] is True

    bad = explore.run({"goal": "g", "questions": ["q1"], "acceptance": ["cites>=3"]},
                      _ctx(tmp_path, chat_fn=fake_chat))
    assert bad["acceptance"]["passed"] is False
    assert "验收未过" in explore.format_results(bad)


def test_writable_worker_calls_approval(tmp_path):
    writes = []

    def fake_chat(ctx, messages, schemas, stop_event):
        if not any("工具执行结果" in str(m.get("content") or "") for m in messages):
            return _reply("", [{"tool": "write_file",
                                "args": {"path": "x.txt", "content": "hi"}}])
        return _reply("结论：写入 x.txt:1")

    res = explore.run({"goal": "g", "questions": [{"ask": "写个文件"}],
                       "allow_write": True},
                      _ctx(tmp_path, write_tool=lambda n, a: (
                          writes.append((n, a)) or (True, "已写入")),
                          chat_fn=fake_chat))
    assert writes and writes[0][0] == "write_file"
    assert res["writes"] == 1
    assert res["results"][0]["ok"] is True


def test_write_cap_rejects_extra(tmp_path):
    def fake_chat(ctx, messages, schemas, stop_event):
        if not any("工具执行结果" in str(m.get("content") or "") for m in messages):
            return _reply("", [{"tool": "write_file",
                                "args": {"path": "a.txt", "content": "1"}},
                               {"tool": "write_file",
                                "args": {"path": "b.txt", "content": "2"}}])
        return _reply("结论：写入 a.txt:1")

    res = explore.run({"goal": "g", "questions": [{"ask": "写两个"}],
                       "allow_write": True},
                      _ctx(tmp_path, write_max=1,
                           write_tool=lambda n, a: (True, "ok"), chat_fn=fake_chat))
    assert res["writes"] == 1


def test_write_disabled_is_rejected(tmp_path):
    def fake_chat(ctx, messages, schemas, stop_event):
        if not any("工具执行结果" in str(m.get("content") or "") for m in messages):
            return _reply("", [{"tool": "write_file",
                                "args": {"path": "a.txt", "content": "1"}}])
        return _reply("结论：未写入")

    res = explore.run({"goal": "g", "questions": [{"ask": "写"}],
                       "allow_write": False},
                      _ctx(tmp_path, write_tool=lambda n, a: (True, "x"),
                           chat_fn=fake_chat))
    assert res["writes"] == 0


def test_blackboard_shares_files():
    b = explore.Blackboard()
    b.note("read_file", {"path": "a.py"}, "第一行\n")
    b.note("search_code", {}, "app/x.py:12: foo\napp/y.py:3: bar")
    brief, ver = b.brief()
    assert "a.py" in brief and "app/x.py" in brief
    assert ver >= 1


def test_batch_persists_and_cold_resumes(monkeypatch, tmp_path):
    monkeypatch.setattr(explore, "DATA_DIR", str(tmp_path))

    def fake_chat(ctx, messages, schemas, stop_event):
        return _reply("结论：入口在 a.py:1")

    ctx = _ctx(tmp_path, chat_fn=fake_chat, session_id="s1", ttl=60)
    res = explore.run({"goal": "g", "questions": [{"ask": "入口"}]}, ctx)
    batch = res["batch"]
    assert batch
    assert (tmp_path / "subagents" / (batch + ".jsonl")).exists()

    with explore._BATCH_LOCK:                      # 模拟进程重启：内存清空
        explore._BATCHES.clear()
    out = explore.followup({"batch": batch, "questions": ["补充"]}, ctx)
    assert out["ok"] is True


# ============================================================
# 第 3 阶段：子会话追问（explore_followup）
# ============================================================
def _clear_batches():
    with explore._BATCH_LOCK:
        explore._BATCHES.clear()


def test_run_returns_batch_and_can_disable(tmp_path):
    _clear_batches()
    ctx = {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
           "budget": 0, "ttl": 60, "chat_fn": _chat_by_role("结论 app/x.py:1")}
    res = explore.run({"goal": "g", "questions": [{"ask": "q"}]}, ctx)
    assert res["batch"].startswith("ex_") and res["ttl"] == 60
    assert "explore_followup" in explore.format_results(res)

    res_off = explore.run({"goal": "g", "questions": [{"ask": "q"}]},
                          dict(ctx, ttl=0))
    assert res_off["batch"] == ""            # 0 = 关闭追问，不缓存


def test_followup_reuses_sub_session(monkeypatch, tmp_path):
    _clear_batches()
    monkeypatch.setattr(agent, "run_tool",
                        lambda s, n, a, **k: ("第一行内容\n第二行内容", None, False))
    seen = []

    def fake_chat(ctx, messages, schemas, stop_event):
        seen.append(list(messages))
        if len(seen) == 1:                   # 第一轮：先读文件
            return {"visible": "", "usage": {"total_tokens": 10},
                    "tool_calls": [{"tool": "search_code", "args": {"query": "入口"}}],
                    "native_fallback": False}
        return {"visible": "结论：入口在 a.py:3", "usage": {"total_tokens": 10},
                "tool_calls": [], "native_fallback": False}

    ctx = {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
           "budget": 0, "ttl": 60, "chat_fn": fake_chat}
    res = explore.run({"goal": "g", "questions": [{"ask": "入口在哪"}]}, ctx)
    batch = res["batch"]
    assert batch

    out = explore.followup({"batch": batch,
                            "questions": [{"ask": "那个函数返回什么"}]}, ctx)
    assert out["ok"] is True and out["batch"] == batch
    last = seen[-1]
    joined = "\n".join(str(m.get("content") or "") for m in last)
    assert "入口在哪" in joined              # 复用原会话的历史问题
    assert "第一行内容" in joined            # 复用已读到的工具结果，不再重新读
    assert "追问" in joined
    assert len(seen) == 3                    # explore 2 轮 + 追问 1 轮
    assert "【子代理追问】" in explore.format_followup(out)


def test_followup_handles_missing_batch_and_disabled(tmp_path):
    _clear_batches()
    ctx = {"sandbox": str(tmp_path), "api_key": "k", "api_url": "http://x", "model": "m",
           "budget": 0, "chat_fn": _chat_by_role("结论")}
    assert "batch" in explore.followup({}, dict(ctx, ttl=60))["error"]

    expired = explore.followup({"batch": "ex_deadbeef", "questions": ["q"]},
                               dict(ctx, ttl=60))
    assert "不存在" in expired["error"]

    off = explore.followup({"batch": "ex_deadbeef", "questions": ["q"]},
                           dict(ctx, ttl=0))
    assert "关闭" in off["error"]


# ============================================================
# 第 4 阶段：嵌套深度 / 后台批次
# ============================================================
def test_nested_tools_only_for_read_roles():
    assert "explore" in explore.ROLE_TOOLS["reader"]
    assert "explore" in explore.ROLE_TOOLS["finder"]
    assert "explore" in explore.ROLE_TOOLS["searcher"]
    assert "explore" not in explore.ROLE_TOOLS["critic"]
    assert "explore" not in explore.ROLE_TOOLS["worker"]
    # 只读 git 工具：子代理要能回答「有没有未提交改动 / 仓库是什么状态」
    assert "git_status" in explore.ROLE_TOOLS["finder"]
    assert "git_status" in explore.ROLE_TOOLS["reader"]
    assert "git_status" in explore.ROLE_TOOLS["critic"]
    assert "置信度" in explore._COMMON


def test_depth_limit_rejects(tmp_path):
    res = explore.run({"goal": "g", "questions": ["q"]},
                      _ctx(tmp_path, depth=4, max_depth=3,
                           chat_fn=lambda *a: _reply("x")))
    assert res["ok"] is False and "深度" in res["error"]


def test_nested_explore_recurses(tmp_path):
    seen = []

    def fake_chat(ctx, messages, schemas, stop_event):
        d = int(ctx.get("depth") or 0)
        seen.append(d)
        if d == 0 and seen.count(0) == 1:
            return _reply("", [{"tool": "explore",
                                "args": {"goal": "孙目标", "questions": ["孙问题"]}}])
        return _reply("结论")

    res = explore.run({"goal": "g", "questions": ["q"]},
                      _ctx(tmp_path, chat_fn=fake_chat, max_depth=3,
                           max_total=12, ttl=0))
    assert res["ok"] is True
    assert 1 in seen


def test_nested_total_cap(tmp_path):
    seen = []

    def fake_chat(ctx, messages, schemas, stop_event):
        last = str(messages[-1].get("content") or "")
        seen.append(last)
        if "【explore】" not in last:
            return _reply("", [{"tool": "explore",
                                "args": {"goal": "x", "questions": ["y"]}}])
        return _reply("结论")

    res = explore.run({"goal": "g", "questions": ["q"]},
                      _ctx(tmp_path, chat_fn=fake_chat, max_depth=3,
                           max_total=1, ttl=0))
    assert res["ok"] is True
    assert any("总数已到上限" in x for x in seen)


def test_background_start_status_result(tmp_path):
    ctx = _ctx(tmp_path, chat_fn=lambda *a: _reply("结论"), ttl=0,
               background_max=3, background_ttl=60)
    r = explore.start_background({"goal": "g", "questions": ["q"]}, ctx)
    assert r["ok"] is True and r["batch"].startswith("ex_")
    st = {}
    for _ in range(100):
        st = explore.status(r["batch"])
        if st.get("status") != "running":
            break
        time.sleep(0.02)
    assert st["status"] == "done"
    out = explore.result(r["batch"])
    assert out["ok"] is True and "结论" in out["text"]
    assert "后台子代理" in explore.format_jobs()


def test_background_cancel(tmp_path):
    def slow_chat(ctx, messages, schemas, stop_event):
        for _ in range(100):
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(0.01)
        return _reply("结论")

    ctx = _ctx(tmp_path, chat_fn=slow_chat, ttl=0, background_max=3)
    r = explore.start_background({"goal": "g", "questions": ["q"]}, ctx)
    assert r["ok"] is True
    assert explore.cancel(r["batch"])["ok"] is True
    st = {}
    for _ in range(100):
        st = explore.status(r["batch"])
        if st.get("status") != "running":
            break
        time.sleep(0.02)
    assert st["status"] == "cancelled"


def test_background_status_rejects_bad_id():
    assert explore.status("nope")["ok"] is False


def test_explore_api_routes_registered():
    """前端靠这三个接口轮询后台批次，路由必须真的注册上。"""
    from app import registry
    import app.api.explore  # noqa: F401
    paths = {p for (_m, p), _fn in registry.snapshot().items()
             if p.startswith("/api/explore/")}
    assert paths == {"/api/explore/status", "/api/explore/result", "/api/explore/cancel"}
    assert explore.result("nope")["ok"] is False
    assert explore.cancel("nope")["ok"] is False

def test_custom_role_tools_prompt_and_model(tmp_path):
    seen = {}

    def fake_chat(ctx, messages, schemas, stop_event, model=None):
        seen["system"] = messages[0]["content"]
        seen["tools"] = [s["function"]["name"] for s in schemas]
        seen["schemas"] = schemas
        seen["model"] = model
        return {"visible": "结论：未发现问题\n置信度: high；未查清: 无",
                "tool_calls": [], "usage": {"total_tokens": 3}}

    ctx = {
        "sandbox": str(tmp_path), "api_key": "k", "api_url": "u", "model": "m",
        "emit": lambda o: None, "budget": 0, "max_agents": 2,
        "chat_fn": fake_chat,
        "roles": {"myrev": {"tools": ["read_file"], "prompt": "自定义提示词",
                            "model": "small-model", "max_turns": 2}},
    }
    res = explore.run({"goal": "审一下", "agents": [{"agent": "myrev", "task": "看 a.py"}]}, ctx)
    assert seen["system"] == "自定义提示词"
    assert seen["tools"] == ["read"]
    read_schema = next(s for s in seen["schemas"]
                       if s["function"]["name"] == "read")
    assert read_schema["function"]["parameters"]["properties"]["action"]["enum"] == ["file"]
    assert seen["model"] == "small-model"
    assert res["ok"] is True
    assert res["results"][0]["role"] == "myrev"


def test_custom_role_rejects_unknown_tool(tmp_path):
    def fake_chat(ctx, messages, schemas, stop_event, model=None):
        return {"visible": "", "tool_calls": [
            {"tool": "write_file", "args": {"path": "x", "content": "y"}}],
            "usage": {"total_tokens": 1}}

    ctx = {
        "sandbox": str(tmp_path), "api_key": "k", "api_url": "u", "model": "m",
        "emit": lambda o: None, "budget": 0, "max_agents": 1, "chat_fn": fake_chat,
        "roles": {"ro": {"tools": ["read_file"], "prompt": "只读", "max_turns": 1}},
    }
    res = explore.run({"goal": "g", "agents": [{"agent": "ro", "task": "t"}]}, ctx)
    assert res["ok"] is False
    assert not (tmp_path / "x").exists()          # 越权工具没有真正执行


# ============================================================
# 并行写隔离（同批不同代理写同一路径）
# ============================================================
def _isolation_ctx(tmp_path, writes, mode, fake_chat):
    def fake_write(name, args):
        writes.append(args.get("path"))
        return True, "已写入"

    return {"sandbox": str(tmp_path), "api_key": "k", "api_url": "u", "model": "m",
            "emit": lambda o: None, "budget": 0, "max_agents": 4,
            "chat_fn": fake_chat, "write_tool": fake_write,
            "write_isolation": mode, "verify_evidence": False}


def _write_once_chat():
    seen = set()

    def fake_chat(ctx, messages, schemas, stop_event, model=None):
        key = id(messages)
        if key not in seen:
            seen.add(key)
            return {"visible": "", "tool_calls": [
                {"tool": "write_file", "args": {"path": "shared.py", "content": "x"}}],
                "usage": {"total_tokens": 1}}
        return {"visible": "完成", "tool_calls": [], "usage": {"total_tokens": 1}}
    return fake_chat


def _two_writers():
    return {"agents": [{"agent": "implementer", "task": "改 shared.py"},
                       {"agent": "documenter", "task": "也改 shared.py"}]}


def test_write_isolation_serial_warns_but_allows(monkeypatch, tmp_path):
    from app import agent_defs
    monkeypatch.setattr(agent_defs, "AGENTS_DIR", str(tmp_path / "agents"))
    writes = []
    res = explore.run(_two_writers(),
                      _isolation_ctx(tmp_path, writes, "serial", _write_once_chat()))
    assert len(writes) == 2
    assert res["write_conflicts"] == 1


def test_write_isolation_strict_rejects_second(monkeypatch, tmp_path):
    from app import agent_defs
    monkeypatch.setattr(agent_defs, "AGENTS_DIR", str(tmp_path / "agents"))
    writes = []
    res = explore.run(_two_writers(),
                      _isolation_ctx(tmp_path, writes, "strict", _write_once_chat()))
    assert len(writes) == 1                 # 第二个写者在 claim 阶段被拒绝
    assert res["write_conflicts"] == 0      # 被拒绝的写入不进写者表


def test_explore_honors_allow_outside(monkeypatch, tmp_path):
    """子代理的 ReadCache 也要把 allow_outside 传下去。"""
    from app import agent_defs
    monkeypatch.setattr(agent_defs, "AGENTS_DIR", str(tmp_path / "agents"))
    box = tmp_path / "box"
    box.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    note = outside / "note.txt"
    note.write_text("OUTSIDE_OK", encoding="utf-8")

    def fake_chat(ctx, messages, schemas, stop_event, model=None):
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "OUTSIDE_OK" in joined:
            return {"visible": "FOUND", "usage": {"total_tokens": 1},
                    "tool_calls": []}
        return {"visible": "", "usage": {"total_tokens": 1},
                "tool_calls": [{"tool": "read_file", "args": {"path": str(note)}}]}

    res = explore.run(
        {"goal": "g", "agents": [{"agent": "reviewer", "task": "读"}]},
        {"sandbox": str(box), "api_key": "k", "api_url": "u", "model": "m",
         "budget": 0, "chat_fn": fake_chat, "allow_outside": True})
    assert "FOUND" in (res["results"][0]["text"] or "")
