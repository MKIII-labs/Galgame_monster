# -*- coding: utf-8 -*-
"""编码评测集：任务加载、mock 判卷、禁止改动校验。"""
import os
import shutil
import sys

from app import eval_coding as ec


def test_load_tasks_has_core_cases():
    tasks = ec.load_tasks()
    names = {t["name"] for t in tasks}
    assert {"fix-add", "implement-slugify", "fix-empty-average"} <= names
    for task in tasks:
        assert os.path.isdir(ec._fixture_path(task)), task["name"]
        assert task["test"], task["name"]


def test_mock_mode_all_tasks_pass():
    """参考解 + 判卷必须全部通过；任何一题失败都说明题目或判卷坏了。"""
    for task in ec.load_tasks():
        res = ec.run_mock(task)
        assert res["ok"], (task["name"], res["reasons"])


def test_live_loop_with_stubbed_model():
    """不花 API：桩模型写文件 + 跑测试 + 结束，验证 live 回路与指标。"""
    task = next(t for t in ec.load_tasks() if t["name"] == "fix-add")
    calls = {"n": 0}

    def fake_stream(api_key, api_url, model, messages, tools=None,
                    timeout=None, **kwargs):
        calls["n"] += 1
        usage = {"prompt": 10, "completion": 5, "total": 15}
        if calls["n"] == 1:
            return {"visible": "", "usage": usage, "tool_calls": [{
                "tool": "write_file",
                "args": {"path": "calc.py",
                         "content": "def add(a, b):\n    return a + b\n"},
            }]}
        if calls["n"] == 2:
            return {"visible": "", "usage": usage, "tool_calls": [{
                "tool": "run_command",
                "args": {"argv": [sys.executable, "test_calc.py"]},
            }]}
        return {"visible": "修好了", "usage": usage, "tool_calls": []}

    orig_conf = ec.get_api_conf
    orig_stream = ec.llm.stream_chat
    ec.get_api_conf = lambda name: ("test-key", "http://127.0.0.1/v1", "test-model")
    ec.llm.stream_chat = fake_stream
    try:
        res = ec.run_live(task, max_rounds=4, timeout=60)
    finally:
        ec.get_api_conf = orig_conf
        ec.llm.stream_chat = orig_stream

    assert res["ok"], res["reasons"]
    assert res["metrics"]["verified"] is True
    assert res["metrics"]["tokens"] == 45


def test_grade_rejects_test_file_edits():
    task = next(t for t in ec.load_tasks() if t["name"] == "fix-add")
    workspace = ec.prepare_workspace(task)
    try:
        before = ec.snapshot(workspace)
        with open(os.path.join(workspace, "test_calc.py"), "a", encoding="utf-8") as f:
            f.write("\n# 偷改测试\n")
        res = ec.grade(task, workspace, before, {"code": 0, "output": ""})
        assert res["ok"] is False
        assert any("禁止" in r for r in res["reasons"])
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_grade_rejects_unexpected_file_change():
    task = next(t for t in ec.load_tasks() if t["name"] == "fix-add")
    workspace = ec.prepare_workspace(task)
    try:
        before = ec.snapshot(workspace)
        with open(os.path.join(workspace, "notes.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        res = ec.grade(task, workspace, before, {"code": 0, "output": ""})
        assert res["ok"] is False
        assert any("预期之外" in r for r in res["reasons"])
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
