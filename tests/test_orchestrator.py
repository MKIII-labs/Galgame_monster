# -*- coding: utf-8 -*-
"""持久化工作流：定义校验、引用、DAG 调度、暂停/恢复、verify。"""
import threading

import pytest

from app import orchestrator as workflow       # 编排层（app/workflow.py 是工作台自动化）


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow, "DATA_DIR", str(tmp_path))
    with workflow._LOCK:
        workflow._LIVE.clear()
    yield
    with workflow._LOCK:
        workflow._LIVE.clear()


def _run(spec, fn, cfg=None, stop_event=None):
    return workflow.start("s1", spec, fn,
                          cfg=dict({"sandbox": ""}, **(cfg or {})),
                          stop_event=stop_event)


# ---------------------------------------------------------------- 校验

def test_normalize_linear_defaults():
    wf, err = workflow.normalize({"name": "x", "steps": [
        {"id": "a", "type": "tool", "tool": "read_file"},
        {"id": "b", "type": "tool", "tool": "read_file"},
        {"id": "c", "type": "tool", "tool": "read_file", "needs": []},
    ]})
    assert err == ""
    assert wf["steps"][0]["needs"] == []
    assert wf["steps"][1]["needs"] == ["a"]          # 默认串到上一个
    assert wf["steps"][2]["needs"] == []             # 显式并行起点


def test_normalize_rejects_bad_defs():
    cases = [
        {"steps": []},
        {"steps": [{"id": "a", "tool": "read_file"},
                   {"id": "a", "tool": "read_file"}]},
        {"steps": [{"id": "a", "needs": ["ghost"], "tool": "read_file"}]},
        {"steps": [{"id": "a", "needs": ["b"], "tool": "read_file"},
                   {"id": "b", "needs": ["a"], "tool": "read_file"}]},
        {"steps": [{"id": "a", "type": "tool"}]},
        {"steps": [{"id": "a", "type": "command"}]},
        {"steps": [{"id": "a", "type": "explore"}]},
        {"steps": [{"id": "a", "type": "verify"}]},
        {"steps": [{"id": "a", "type": "nope"}]},
        {"steps": [{"id": "bad id!", "tool": "read_file"}]},
    ]
    for spec in cases:
        wf, err = workflow.normalize(spec)
        assert wf is None and err, spec


def test_render_and_when():
    wf, _ = workflow.normalize({"steps": [
        {"id": "a", "tool": "read_file"},
        {"id": "b", "tool": "read_file"},
    ]})
    wf["steps"][0]["status"] = "done"
    wf["steps"][0]["output"] = "hello"
    wf["steps"][0]["exit_code"] = 0
    assert workflow._render("x {{a.output}}", wf) == "x hello"
    assert workflow._render("{{a.exit_code}}", wf) == "0"
    assert workflow._when_ok("{{a.status}} == done", wf) is True
    assert workflow._when_ok("{{a.exit_code}} != 0", wf) is False
    assert workflow._when_ok("{{a.output}} contains ell", wf) is True
    assert workflow._when_ok("{{ghost.output}} == 1", wf) is False


# ---------------------------------------------------------------- 执行

def test_linear_execution_and_final():
    calls, seen_args = [], {}

    def ex(node):
        calls.append(node["id"])
        seen_args[node["id"]] = node.get("args")
        return {"ok": True, "output": "out-" + node["id"]}

    state = _run({"name": "t", "steps": [
        {"id": "a", "type": "tool", "tool": "read_file"},
        {"id": "b", "type": "tool", "tool": "read_file",
         "args": {"path": "{{a.output}}.txt"}},
        {"id": "f", "type": "final", "template": "A={{a.output}} B={{b.output}}"},
    ]}, ex)
    assert state["status"] == "completed" and state["ok"] is True
    assert calls == ["a", "b"]
    assert seen_args["b"] == {"path": "out-a.txt"}     # 引用先渲染再交给执行器
    final = next(s for s in state["steps"] if s["id"] == "f")
    assert final["output"] == "A=out-a B=out-b"


def test_dag_parallel_readonly():
    bar = threading.Barrier(2, timeout=3)

    def ex(node):
        bar.wait()          # 串行执行会在这里超时
        return {"ok": True, "output": node["id"]}

    state = _run({"steps": [
        {"id": "a", "tool": "read_file", "needs": []},
        {"id": "b", "tool": "read_file", "needs": []},
    ]}, ex)
    assert state["status"] == "completed"


def test_failure_skips_dependents():
    state = _run({"steps": [
        {"id": "a", "tool": "read_file"},
        {"id": "b", "tool": "read_file"},
        {"id": "c", "tool": "read_file", "needs": []},
    ]}, lambda n: {"ok": n["id"] != "a", "output": n["id"]})
    statuses = {s["id"]: s["status"] for s in state["steps"]}
    assert statuses["a"] == "failed"
    assert statuses["b"] == "skipped"
    assert statuses["c"] == "done"
    assert state["status"] == "failed"


def test_when_skips_node():
    state = _run({"steps": [
        {"id": "a", "tool": "read_file"},
        {"id": "b", "tool": "read_file", "when": "{{a.output}} contains 不存在的词"},
    ]}, lambda n: {"ok": True, "output": "yes"})
    statuses = {s["id"]: s["status"] for s in state["steps"]}
    assert statuses["a"] == "done" and statuses["b"] == "skipped"
    assert state["status"] == "completed"        # 跳过不算失败


def test_retry_then_success():
    count = {"n": 0}

    def ex(node):
        count["n"] += 1
        return {"ok": count["n"] >= 2, "output": "x"}

    state = _run({"steps": [{"id": "a", "tool": "read_file", "retries": 1}]}, ex)
    assert state["status"] == "completed" and count["n"] == 2


def test_pause_then_resume(monkeypatch):
    monkeypatch.setattr(workflow, "new_id", lambda: "wf_00000001")
    calls = []

    def ex(node):
        calls.append(node["id"])
        if node["id"] == "a":
            workflow.pause("wf_00000001")
        return {"ok": True, "output": node["id"]}

    spec = {"steps": [{"id": "a", "tool": "read_file"},
                      {"id": "b", "tool": "read_file"}]}
    st = _run(spec, ex)
    assert st["status"] == "paused" and calls == ["a"]

    st2 = workflow.resume("s1", "wf_00000001", ex, cfg={"sandbox": ""})
    assert st2["status"] == "completed"
    assert calls == ["a", "b"]          # 已完成的节点不重跑


def test_stop_marks_interrupted_then_resume():
    ev = threading.Event()
    calls = []

    def ex(node):
        calls.append(node["id"])
        ev.set()                        # 第一个节点跑完就「停止」
        return {"ok": True, "output": node["id"]}

    spec = {"steps": [{"id": "a", "tool": "read_file"},
                      {"id": "b", "tool": "read_file"}]}
    st = _run(spec, ex, stop_event=ev)
    assert st["status"] == "interrupted"
    assert calls == ["a"]

    st2 = workflow.resume("s1", st["id"], ex, cfg={"sandbox": ""})
    assert st2["status"] == "completed"
    assert calls == ["a", "b"]


def test_cancel_flag(monkeypatch):
    monkeypatch.setattr(workflow, "new_id", lambda: "wf_00000002")

    def ex(node):
        workflow.cancel("wf_00000002")
        return {"ok": True, "output": node["id"]}

    spec = {"steps": [{"id": "a", "tool": "read_file"},
                      {"id": "b", "tool": "read_file"}]}
    st = _run(spec, ex)
    assert st["status"] == "cancelled"
    assert {s["id"]: s["status"] for s in st["steps"]}["b"] == "pending"


def test_budget_pauses():
    def ex(node):
        return {"ok": True, "output": "x", "tokens": 10}

    spec = {"budget": {"tokens": 10},
            "steps": [{"id": "a", "tool": "read_file"},
                      {"id": "b", "tool": "read_file"}]}
    st = _run(spec, ex)
    assert st["status"] == "paused"
    assert st["used"]["tokens"] == 10
    assert {s["id"]: s["status"] for s in st["steps"]}["b"] == "pending"


# ---------------------------------------------------------------- verify

def test_verify_pass_and_fail(tmp_path):
    (tmp_path / "x.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    ok = _run({"steps": [{"id": "v", "type": "verify", "checks": [
        {"path": "x.py", "contains": "def foo"},
        {"path": "missing.txt", "exists": False},
    ]}]}, lambda n: {"ok": True}, cfg={"sandbox": str(tmp_path)})
    assert ok["status"] == "completed"

    bad = _run({"steps": [{"id": "v", "type": "verify", "checks": [
        {"path": "x.py", "contains": "not-there"},
    ]}]}, lambda n: {"ok": True}, cfg={"sandbox": str(tmp_path)})
    assert bad["status"] == "failed"


def test_verify_rejects_path_escape(tmp_path):
    st = _run({"steps": [{"id": "v", "type": "verify", "checks": [
        {"path": "../../secret.txt", "exists": True},
    ]}]}, lambda n: {"ok": True}, cfg={"sandbox": str(tmp_path)})
    assert st["status"] == "failed"
    assert "越出沙箱" in (st["steps"][0]["output"] + st["steps"][0]["error"])


# ---------------------------------------------------------------- 存储 / 展示

def test_persist_and_load_guards():
    st = _run({"name": "t", "steps": [{"id": "a", "tool": "read_file"}]},
              lambda n: {"ok": True, "output": "ok"})
    loaded = workflow.load(st["id"])
    assert loaded and loaded["status"] == "completed"
    assert workflow.load("../../etc/passwd") is None
    assert workflow.load("wf_zzzzzzzz") is None


def test_summary_text_has_steps_and_final():
    st = _run({"name": "整理", "steps": [
        {"id": "a", "tool": "read_file"},
        {"id": "f", "type": "final", "template": "done {{a.output}}"},
    ]}, lambda n: {"ok": True, "output": "ok"})
    text = workflow.summary_text(st)
    assert "整理" in text and "[a/tool]✓" in text and "done ok" in text


# ---------------------------------------------------------------- 复合节点

def test_when_supports_comparisons_and_logic():
    wf, _ = workflow.normalize({"steps": [{"id": "a", "tool": "read_file"}]})
    wf["steps"][0]["status"] = "done"
    wf["steps"][0]["output"] = "5"
    assert workflow._when_ok("{{a.output}} > 3", wf) is True
    assert workflow._when_ok("{{a.output}} <= 5", wf) is True
    assert workflow._when_ok("{{a.output}} > 9", wf) is False
    assert workflow._when_ok("{{a.output}} > 3 && {{a.status}} == done", wf) is True
    assert workflow._when_ok("{{a.output}} > 9 || {{a.status}} == done", wf) is True
    assert workflow._when_ok("!{{a.status}} == done", wf) is False


def test_loop_executes_each_item():
    seen = []

    def ex(node):
        seen.append(node.get("args", {}).get("x"))
        return {"ok": True, "output": "v-" + str(node.get("args", {}).get("x"))}

    state = _run({"steps": [
        {"id": "l", "type": "loop", "items": ["a", "b", "c"],
         "steps": [{"id": "inner", "type": "tool", "tool": "read_file",
                    "args": {"x": "{{item}}"}}]},
    ]}, ex)
    assert state["status"] == "completed"
    assert seen == ["a", "b", "c"]
    node = state["steps"][0]
    assert len(node["children"]) == 3
    assert node["children"][0]["output"] == "v-a"


def test_loop_until_stops_early():
    state = _run({"steps": [
        {"id": "l", "type": "loop", "items": [1, 2, 3, 4],
         "until": "{{index}} >= 1",
         "steps": [{"id": "inner", "type": "tool", "tool": "read_file"}]},
    ]}, lambda n: {"ok": True, "output": "x"})
    assert len(state["steps"][0]["children"]) == 2       # index 0、1


def test_loop_on_error_continue():
    state = _run({"steps": [
        {"id": "l", "type": "loop", "items": ["a", "b"], "on_error": "continue",
         "steps": [{"id": "inner", "type": "tool", "tool": "read_file",
                    "args": {"x": "{{item}}"}}]},
    ]}, lambda n: {"ok": n.get("args", {}).get("x") != "a",
                   "output": str(n.get("args", {}).get("x"))})
    assert state["status"] == "completed"
    assert len(state["steps"][0]["children"]) == 2


def test_map_fans_out_and_collects():
    state = _run({"steps": [
        {"id": "m", "type": "map", "items": ["a", "b", "c"], "parallel": 3,
         "collect": "join",
         "node": {"type": "tool", "tool": "read_file",
                  "args": {"x": "{{item}}"}}},
    ]}, lambda n: {"ok": True, "output": str(n.get("args", {}).get("x"))})
    node = state["steps"][0]
    assert state["status"] == "completed"
    assert len(node["children"]) == 3
    assert node["output"].split("\n") == ["a", "b", "c"]


def test_condition_picks_branch():
    calls = []

    def ex(node):
        calls.append(node["id"])
        return {"ok": True, "output": node["id"]}

    state = _run({"steps": [
        {"id": "c", "type": "condition", "cond": "1 == 1",
         "then": [{"id": "t1", "type": "tool", "tool": "read_file"}],
         "else": [{"id": "e1", "type": "tool", "tool": "read_file"}]},
    ]}, ex)
    assert state["status"] == "completed"
    assert calls == ["t1"]
    assert state["steps"][0]["output"] == "t1"


def test_normalize_rejects_empty_composites():
    cases = [
        {"steps": [{"id": "l", "type": "loop", "items": [1]}]},
        {"steps": [{"id": "m", "type": "map", "items": [1]}]},
        {"steps": [{"id": "c", "type": "condition"}]},
    ]
    for spec in cases:
        wf, err = workflow.normalize(spec)
        assert wf is None and err, spec
