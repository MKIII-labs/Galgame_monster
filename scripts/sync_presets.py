# -*- coding: utf-8 -*-
"""把随项目发布的示例素材 resources/presets/** 同步到运行时目录 data/**。

    python scripts/sync_presets.py                # 只补缺失（等于启动时的自动播种）
    python scripts/sync_presets.py --force        # 用 presets 覆盖 data 里的同名文件
    python scripts/sync_presets.py --dry-run      # 只报告会做什么，不落盘

为什么要它：presets 是「随项目分发」的、data 是「本机运行时」的，两边同名但用途不同。
改了 presets 里的角色/知识库/背景后，用 --force 一键推到 data，不用手工复制。

约定（与 app/presets.py 一致）：
- .md 说明文件不播种（CREDITS.md 只在 presets 里）
- knowledge/index.json 永远按条目 id **合并**，不整文件覆盖（避免冲掉用户自建的知识库）
"""
import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.presets import PRESETS_DIR, _merge_knowledge_index          # noqa: E402
from app.store import DATA_DIR                                        # noqa: E402


def _iter_preset_files(presets_dir):
    for dirpath, _dirs, files in os.walk(presets_dir):
        rel_dir = os.path.relpath(dirpath, presets_dir)
        for fn in files:
            rel = os.path.normpath(os.path.join(rel_dir, fn)).replace(os.sep, "/")
            if rel == "knowledge/index.json":
                continue                       # 索引单独合并
            if rel.lower().endswith(".md"):
                continue                       # 说明类文件不播种
            yield rel, os.path.join(dirpath, fn)


def sync(data_dir=None, presets_dir=None, force=False, dry_run=False):
    """返回 (复制, 跳过, 索引新增)"""
    data = data_dir or DATA_DIR
    pre = presets_dir or PRESETS_DIR
    if not os.path.isdir(pre):
        print("找不到 presets 目录：%s" % pre)
        return 0, 0, 0
    copied = skipped = 0
    for rel, src in _iter_preset_files(pre):
        dst = os.path.join(data, *rel.split("/"))
        if os.path.exists(dst) and not force:
            skipped += 1
            continue
        verb = "覆盖" if os.path.exists(dst) else "新增"
        print("  %s  %s" % (verb, rel))
        if not dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        copied += 1
    added = 0
    if not dry_run:
        added = _merge_knowledge_index(data, pre)
    return copied, skipped, added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="同名文件也用 presets 覆盖")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不落盘")
    args = ap.parse_args()

    print("presets: %s\ndata   : %s\n模式   : %s%s\n" % (
        PRESETS_DIR, DATA_DIR,
        "强制覆盖" if args.force else "只补缺失",
        "（dry-run）" if args.dry_run else ""))
    copied, skipped, added = sync(force=args.force, dry_run=args.dry_run)
    print("\n结果：复制/覆盖 %d 个，跳过已存在 %d 个，知识库索引新增 %d 条" % (copied, skipped, added))
    if not args.force and skipped:
        print("提示：要让 presets 的改动生效，加 --force 再跑一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
