# -*- coding: utf-8 -*-
"""全局权限规则与预设：写入 / 备份 / 合并优先级。"""
import json
import os
from importlib import import_module

import pytest

from app import agent, agent_presets

# 包拆分后全局规则读写搬到了 app.agent.rules，DATA_DIR 要在定义处打补丁
agent_rules = import_module("app.agent.rules")


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(agent, "DATA_DIR", str(d))
    monkeypatch.setattr(agent_rules, "DATA_DIR", str(d))
    return d


def test_apply_backup_and_clear(data_dir):
    path, bak = agent_presets.apply("standard")
    assert path == str(data_dir / "agent-rules.json")
    assert os.path.isfile(path) and bak == ""

    rules, err = agent.load_global_rules()
    assert err == ""
    assert "git commit*" in rules["commands"]["allow"]
    assert "git push*" in rules["commands"]["ask"]

    _p, bak2 = agent_presets.apply("cautious")
    assert bak2 and os.path.isfile(bak2)          # 覆盖前备份旧文件
    rules2, _ = agent.load_global_rules()
    assert rules2["commands"]["ask"] == ["*"]

    _p, bak3 = agent_presets.clear()
    assert bak3 and not os.path.isfile(agent.global_rules_path())


def test_unknown_preset_raises(data_dir):
    with pytest.raises(ValueError):
        agent_presets.apply("nope")


def test_preset_names_include_three_tiers():
    names = [p["id"] for p in agent_presets.names()]
    assert names == ["standard", "cautious", "readonly"]


def test_effective_rules_merge_and_priority(data_dir, tmp_path):
    sandbox = tmp_path / "proj"
    sandbox.mkdir()
    (data_dir / "agent-rules.json").write_text(json.dumps({
        "allow": ["docs/**"],
        "commands": {"allow": ["python -m pytest*"]},
    }, ensure_ascii=False), encoding="utf-8")
    gm = sandbox / ".gm"
    gm.mkdir()
    (gm / "agent-rules.json").write_text(json.dumps({
        "deny": ["docs/secret/**"],
        "commands": {"deny": ["git push*"]},
    }, ensure_ascii=False), encoding="utf-8")

    rules, err = agent.load_effective_rules(str(sandbox))
    assert err == ""
    assert "docs/**" in rules["allow"] and "docs/secret/**" in rules["deny"]

    # 全局 allow 生效
    assert agent.judge_command(str(sandbox),
                               {"argv": ["python", "-m", "pytest", "-q"]},
                               "custom")[0] == "allow"
    # 项目 deny 压过全局 allow
    assert agent.judge_write(str(sandbox), "write_file",
                             {"path": "docs/secret/a.md"}, "custom")[0] == "deny"


def test_broken_global_rules_fail_closed(data_dir, tmp_path):
    sandbox = tmp_path / "proj2"
    sandbox.mkdir()
    (data_dir / "agent-rules.json").write_text("{bad json", encoding="utf-8")
    rules, err = agent.load_effective_rules(str(sandbox))
    assert rules == {} and "无法解析" in err
    # 规则坏了 → 按「必须询问」兜底，不能变成放行
    assert agent.judge_write(str(sandbox), "write_file",
                             {"path": "a.txt", "content": "x"}, "ai")[0] == "ask"
