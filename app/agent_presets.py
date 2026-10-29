# -*- coding: utf-8 -*-
"""权限规则预设：一键写入全局 data/agent-rules.json。

设计原则：
- 预设只写「规则」，不碰命令白名单、秘密文件护栏等硬边界；
- 三档覆盖日常开发 / 谨慎 / 只读审查；
- 写入是覆盖式的，应用前先把旧文件备份成 .bak，避免手写规则被误删。
"""
import json
import os
import shutil
import time

from app import agent

PRESETS = {
    "standard": {
        "name": "标准开发",
        "desc": "测试 / 构建 / 本地 Git 提交放行；装依赖、推送、改历史才问",
        "rules": {
            "allow": ["docs/**", "README.md"],
            "ask": [],
            "deny": [],
            "commands": {
                "allow": [
                    "python -m pytest*", "pytest*",
                    "python *", "python3 *", "py *",
                    "node *", "npm run *", "npm test*",
                    "git status*", "git log*", "git diff*",
                    "git add*", "git commit*",
                    "ruff check*", "ruff format*",
                ],
                "ask": [
                    "pip install*", "pip3 install*", "python -m pip*",
                    "npm install*", "npx *",
                    "git push*", "git reset*", "git clean*",
                    "git checkout*", "git switch*", "git rebase*",
                ],
                "deny": [],
            },
        },
    },
    "cautious": {
        "name": "谨慎",
        "desc": "任何写入和命令都先问你",
        "rules": {
            "allow": [],
            "ask": ["**"],
            "deny": [],
            "commands": {"allow": [], "ask": ["*"], "deny": []},
        },
    },
    "readonly": {
        "name": "只读审查",
        "desc": "只允许读取与搜索；写入和命令一律拒绝",
        "rules": {
            "allow": [],
            "ask": [],
            "deny": ["**"],
            "commands": {"allow": [], "ask": [], "deny": ["*"]},
        },
    },
}


def names():
    """给前端下拉/按钮用的预设清单（保持声明顺序）。"""
    return [{"id": k, "name": v["name"], "desc": v["desc"], "rules": v["rules"]}
            for k, v in PRESETS.items()]


def _backup(path):
    bak = path + ".%s.bak" % time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copyfile(path, bak)
        return bak
    except OSError:
        return ""


def apply(preset):
    """把预设写入全局规则文件；返回 (path, 备份路径或空串)。未知预设抛 ValueError。"""
    key = str(preset or "").strip().lower()
    item = PRESETS.get(key)
    if not item:
        raise ValueError("未知预设：%s" % preset)
    path = agent.global_rules_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bak = _backup(path) if os.path.isfile(path) else ""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(item["rules"], f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path, bak


def clear():
    """删除全局规则文件（等同于「不设全局规则」）；返回 (path, 备份路径)。"""
    path = agent.global_rules_path()
    if not os.path.isfile(path):
        return path, ""
    bak = _backup(path)
    try:
        os.remove(path)
    except OSError:
        pass
    return path, bak
