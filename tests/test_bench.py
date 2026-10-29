# -*- coding: utf-8 -*-
"""评测框架：任务集校验、断言求值、消融过滤、摘要与盲评页"""
import json
import os
import sys

from PIL import Image

from app import bench


def test_task_set_is_valid():
    assert bench.validate() == []


def test_dry_run_all_ok():
    rows = bench.dry_run()
    assert rows and all(r["ok"] for r in rows)
    ids = {r["task"] for r in rows}
    assert {"bug_fix", "refactor", "tool_pipeline", "asset_mc",
            "web_voxel", "ui_restore"} <= ids


def test_expect_files(tmp_path):
    (tmp_path / "a.txt").write_text("hello 世界", encoding="utf-8")
    (tmp_path / "s.json").write_text(json.dumps({"01.txt": "Alpha"}), encoding="utf-8")
    Image.new("RGB", (32, 32), (10, 120, 30)).save(tmp_path / "atlas.png")

    checks = bench.evaluate({"expect": {"files": [
        {"path": "a.txt", "min_bytes": 3, "text_contains": "世界"},
        {"path": "a.txt", "text_not_contains": "不存在"},
        {"path": "s.json", "json_keys": 1},
        {"path": "atlas.png", "image": {"width_min": 32, "height_min": 32}},
    ]}}, str(tmp_path), {})
    assert all(c["ok"] for c in checks), checks


def test_expect_files_failures(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "b.json").write_text("[]", encoding="utf-8")
    checks = bench.evaluate({"expect": {"files": [
        {"path": "missing.txt"},
        {"path": "a.txt", "min_bytes": 99},
        {"path": "a.txt", "text_contains": "没有"},
        {"path": "b.json", "json_keys": 1},
    ]}}, str(tmp_path), {})
    assert [c["ok"] for c in checks] == [False, False, False, False]


def test_expect_command(tmp_path):
    ok = bench._check_command(str(tmp_path), {"argv": [sys.executable, "-c",
                                                       "print('ok')"], "exit": 0})
    assert ok["ok"]
    bad = bench._check_command(str(tmp_path), {"argv": [sys.executable, "-c",
                                                        "raise SystemExit(3)"], "exit": 0})
    assert not bad["ok"]


def test_expect_tools():
    result = {"tools": [{"tool": "read_file"}, {"tool": "write_file"}]}
    assert bench._check_tools({"require": ["write_file"], "min": 2}, result)["ok"]
    assert not bench._check_tools({"require": ["run_command"]}, result)["ok"]
    assert bench._check_tools({"require_any": ["run_command", "read_file"]}, result)["ok"]
    assert not bench._check_tools({"require_any": ["run_command"]}, result)["ok"]


def test_ablation_filter():
    assert "visual_check" in bench.ablation_disable_tools(["no_visual"])
    assert "asset_atlas" in bench.ablation_disable_tools(["no_assets"])
    names = {s["function"]["name"] for s in bench._schemas(["no_visual"])}
    assert "visual_check" not in names
    assert "write_file" in names


def test_summarize():
    summary = bench.summarize([
        {"task": "a", "status": "finished", "ok": True, "checks": [],
         "usage": {"total_tokens": 100}, "ms": 10},
        {"task": "b", "status": "finished", "ok": False,
         "checks": [{"name": "cmd", "ok": False}],
         "usage": {"total_tokens": 50}, "ms": 20},
    ])
    assert summary["runnable"] == 2
    assert summary["passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["tokens"] == 150
    assert summary["tasks"][1]["failed_checks"] == ["cmd"]
    assert summary["by_ablation"]["baseline"] == {"total": 2, "passed": 1,
                                                  "pass_rate": 0.5}


def test_render_review(tmp_path):
    for name, final in (("run_a", "方案一的结果"), ("run_b", "方案二的结果")):
        d = tmp_path / name
        d.mkdir()
        (d / "bug_fix.json").write_text(json.dumps({
            "task": "bug_fix", "status": "finished", "ok": name == "run_a",
            "turns": 3, "tool_calls": 2, "usage": {"total_tokens": 10},
            "final": final, "checks": [], "tools": [],
        }, ensure_ascii=False), encoding="utf-8")
    out = bench.render_review(str(tmp_path / "run_a"), str(tmp_path / "run_b"),
                              str(tmp_path / "review.html"))
    html = open(out, encoding="utf-8").read()
    assert "方案 A" in html and "方案 B" in html
    assert "方案一的结果" in html and "方案二的结果" in html
    assert "run_a" not in html          # 匿名：不暴露运行目录名


def test_run_task_without_api_key(monkeypatch):
    monkeypatch.setattr(bench, "get_api_conf", lambda kind: ("", "", "m"))
    r = bench.run_task("bug_fix")
    assert r["status"] == "no_api_key"
    assert r["ok"] is False


def test_save_run_writes_summary(tmp_path):
    results = [{"task": "a", "status": "finished", "ok": True, "checks": [],
                "turns": 1, "tool_calls": 1, "usage": {"total_tokens": 5},
                "ms": 10, "final": "ok", "tools": []}]
    d, summary = bench.save_run(results, {"model": "m", "run_id": "r1"},
                                root=str(tmp_path))
    assert os.path.isfile(os.path.join(d, "a.json"))
    assert os.path.isfile(os.path.join(d, "summary.json"))
    assert os.path.isfile(os.path.join(d, "summary.md"))
    assert summary["passed"] == 1
    assert "pass_rate" in summary


def test_run_task_end_to_end_with_fake_model(tmp_path, monkeypatch):
    """不调真实模型：用假 stream_chat 走完「调工具 → 回灌 → 断言」全链路。"""
    state = {"n": 0}

    def fake_stream(api_key, api_url, model, messages, **_kw):
        state["n"] += 1
        if state["n"] == 1:
            return {"visible": "", "tool_calls": [
                {"tool": "write_file",
                 "args": {"path": "out.txt", "content": "hello"}}],
                "usage": {"total_tokens": 10}}
        return {"visible": "done", "tool_calls": [], "usage": {"total_tokens": 5}}

    monkeypatch.setattr(bench.llm, "stream_chat", fake_stream)
    monkeypatch.setattr(bench, "get_api_conf", lambda kind: ("k", "u", "m"))
    task = {"id": "t_end", "prompt": "写 out.txt", "max_turns": 3, "timeout": 60,
            "fixture": "asset_mc",
            "expect": {"files": [{"path": "out.txt", "text_contains": "hello"}]}}
    r = bench.run_task(task, workdir=str(tmp_path / "w"), keep=True)
    assert r["status"] == "finished"
    assert r["ok"] is True
    assert r["turns"] == 1
    assert r["tool_calls"] == 1
    assert (tmp_path / "w" / "out.txt").read_text(encoding="utf-8") == "hello"
