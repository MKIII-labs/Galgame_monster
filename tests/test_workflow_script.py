# -*- coding: utf-8 -*-
"""P2：脚本化工作流（wf.call / wf.parallel / wf.workflow）。"""
import os

from app import workflow_script


def _box(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    return str(tmp_path)


def test_wf_call_and_workflow(tmp_path):
    box = _box(tmp_path)
    calls = []

    def fake_wf(spec):
        calls.append(spec)
        return {"ok": True, "id": "wf_test", "status": "completed",
                "steps": [{"id": "a", "status": "done", "output": "ok"}]}

    code = ("import wf\n"
            "print(wf.call('read_file', path='a.txt'))\n"
            "print(wf.workflow_text({'name': 'x'}))\n")
    text, stat = workflow_script.run(
        box, code, caps={"sid": "s1", "run_workflow": fake_wf}, timeout=20)
    assert "hello" in text
    assert "wf_test" in text
    assert stat["workflows"] == 1
    assert calls and calls[0]["name"] == "x"


def test_wf_denies_write_by_default(tmp_path):
    box = _box(tmp_path)
    code = ("import wf\n"
            "try:\n"
            "    wf.call('write_file', path='evil.txt', content='x')\n"
            "except wf.ToolError as e:\n"
            "    print('DENIED', e.code)\n")
    text, _ = workflow_script.run(box, code, timeout=20)
    assert "DENIED TOOL_NOT_ALLOWED" in text
    assert not os.path.exists(os.path.join(box, "evil.txt"))


def test_wf_workflow_unavailable(tmp_path):
    box = _box(tmp_path)
    code = ("import wf\n"
            "try:\n"
            "    wf.workflow({})\n"
            "except wf.ToolError as e:\n"
            "    print('CODE', e.code)\n")
    text, _ = workflow_script.run(box, code, timeout=20)
    assert "CODE WORKFLOW_UNAVAILABLE" in text
