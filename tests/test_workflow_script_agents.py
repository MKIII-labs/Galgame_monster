# -*- coding: utf-8 -*-
"""脚本编排：wf.agent / wf.agents / wf.pipeline 的桥接与限额。"""
from app import workflow_script


def _caps(fn, limit=8):
    return {"run_agents": fn, "agent_script_agent_calls": limit}


def test_wf_agent_returns_text(tmp_path):
    seen = []

    def fake(payload):
        seen.append(payload)
        return {"ok": True, "text": "结论：没问题"}

    code = (
        "import wf\n"
        "print(wf.agent('reviewer', '审查 app/api', goal='检查'))\n"
    )
    text, stat = workflow_script.run(str(tmp_path), code, caps=_caps(fake, 4))
    assert "结论：没问题" in text
    assert stat["agents"] == 1 and stat["ok"] is True
    assert seen[0]["agents"][0] == {"agent": "reviewer", "task": "审查 app/api"}
    assert seen[0]["goal"] == "检查"


def test_wf_agents_and_pipeline_payload(tmp_path):
    seen = []

    def fake(payload):
        seen.append(payload)
        return {"ok": True, "text": "ok"}

    code = (
        "import wf\n"
        "print(wf.agents([{'agent': 'tester', 'task': '看测试'}]))\n"
        "print(wf.pipeline(['planner', 'implementer'], '做一件事'))\n"
    )
    text, stat = workflow_script.run(str(tmp_path), code, caps=_caps(fake))
    assert stat["agents"] == 2
    assert seen[0]["agents"][0]["agent"] == "tester"
    assert seen[1]["pipeline"] == ["planner", "implementer"]
    assert seen[1]["task"] == "做一件事"


def test_agent_call_budget(tmp_path):
    def fake(payload):
        return {"ok": True, "text": "ok"}

    code = (
        "import wf\n"
        "for i in range(5):\n"
        "    try:\n"
        "        wf.agent('reviewer', 'x%d' % i)\n"
        "    except wf.ToolError as e:\n"
        "        print('ERR', e.code)\n"
        "print('done')\n"
    )
    text, stat = workflow_script.run(str(tmp_path), code, caps=_caps(fake, 2))
    assert stat["agents"] == 2
    assert "AGENT_BUDGET" in text
    assert "done" in text


def test_agent_unavailable_message(tmp_path):
    code = (
        "import wf\n"
        "try:\n"
        "    wf.agent('reviewer', 'x')\n"
        "except wf.ToolError as e:\n"
        "    print('ERR', e.code, e)\n"
    )
    text, stat = workflow_script.run(str(tmp_path), code, caps={})
    assert "AGENT_UNAVAILABLE" in text
    assert stat["agents"] == 0


def test_agent_failure_surfaces_error(tmp_path):
    def fake(payload):
        return {"ok": False, "error": "未知或已停用：nope"}

    code = (
        "import wf\n"
        "try:\n"
        "    wf.agent('nope', 'x')\n"
        "except wf.ToolError as e:\n"
        "    print('ERR', e.code, e)\n"
    )
    text, _stat = workflow_script.run(str(tmp_path), code, caps=_caps(fake))
    assert "AGENTS_FAILED" in text and "未知或已停用" in text


def test_script_requires_sandbox(tmp_path):
    text, stat = workflow_script.run("", "print(1)", caps=_caps(lambda p: {}))
    assert "沙箱" in text
    assert stat["calls"] == 0 and stat["agents"] == 0


def test_wf_call_honors_allow_outside(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    note = tmp_path / "outside.txt"
    note.write_text("WF_OUTSIDE_OK", encoding="utf-8")
    code = "import wf\nprint(wf.call('read_file', path=%r))\n" % str(note)
    text, _stat = workflow_script.run(str(box), code, allow_outside=True)
    assert "WF_OUTSIDE_OK" in text
