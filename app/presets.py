# -*- coding: utf-8 -*-
"""示例素材播种：把随项目发布的 resources/presets/** 复制到运行时目录 data/**。

规则：
- 只复制**目标不存在**的文件，绝不覆盖用户已有数据；
- knowledge/index.json 特殊处理：按条目（id）**合并**，而不是整文件覆盖。

这样示例角色 / 知识库 / 背景 / 立绘 / BGM 能随项目分发，而用户自己的会话、密钥、
画像等数据仍只留在本机 `data/`。
"""
import os
import json
import shutil

from app.store import DATA_DIR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRESETS_DIR = os.path.join(ROOT, "resources", "presets")


def _copy_missing(src, dst):
    if os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def default_presets_dir():
    return PRESETS_DIR


def seed_presets(data_dir=None, presets_dir=None):
    """把示例素材播种到 data/（缺失才复制）。返回复制的文件数。"""
    data = data_dir or DATA_DIR
    pre = presets_dir or PRESETS_DIR
    if not os.path.isdir(pre):
        return 0
    copied = 0
    for dirpath, _dirs, files in os.walk(pre):
        rel_dir = os.path.relpath(dirpath, pre)
        for fn in files:
            rel = os.path.normpath(os.path.join(rel_dir, fn)).replace(os.sep, "/")
            if rel == "knowledge/index.json":
                continue                       # 知识库索引单独合并
            if rel.lower().endswith(".md"):
                continue                       # 说明类文件（如 CREDITS.md）不播种到 data/
            if _copy_missing(os.path.join(dirpath, fn), os.path.join(data, *rel.split("/"))):
                copied += 1
    copied += _merge_knowledge_index(data, pre)
    return copied


def _merge_knowledge_index(data_dir, presets_dir):
    """把预设 index.json 里「用户还没有」的知识库条目并进去；返回新增条目数。"""
    pre_idx = os.path.join(presets_dir, "knowledge", "index.json")
    if not os.path.isfile(pre_idx):
        return 0
    try:
        with open(pre_idx, "r", encoding="utf-8") as f:
            pdata = json.load(f) or {}
    except Exception:
        return 0
    pitems = [k for k in (pdata.get("kbs") or []) if isinstance(k, dict) and k.get("id")]
    if not pitems:
        return 0

    cur_idx = os.path.join(data_dir, "knowledge", "index.json")
    cur = {}
    if os.path.isfile(cur_idx):
        try:
            with open(cur_idx, "r", encoding="utf-8") as f:
                cur = json.load(f) or {}
        except Exception:
            cur = {}
    cur.setdefault("kbs", [])
    have = {k.get("id") for k in cur["kbs"] if isinstance(k, dict)}
    added = 0
    for it in pitems:
        if it["id"] not in have:
            cur["kbs"].append(it)
            have.add(it["id"])
            added += 1
    if added or not os.path.isfile(cur_idx):
        os.makedirs(os.path.dirname(cur_idx), exist_ok=True)
        tmp = cur_idx + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cur_idx)
    return added
