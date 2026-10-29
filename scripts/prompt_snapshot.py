# -*- coding: utf-8 -*-
"""P2 快照：把 6 个提示词构造函数在多种输入下的输出存成 JSON，供重构后逐字节对比。

用法：
    python scripts/prompt_snapshot.py save    # 重构前：存基线
    python scripts/prompt_snapshot.py check   # 重构后：比对
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import gal                                        # noqa: E402

SNAP = os.path.join(ROOT, "scripts", "_prompt_snapshot.json")

CHAR = {
    "name": "测试角色",
    "profile": "设定：普通角色的设定文本。\n第二行。",
    "sprites": [
        {"path": "sprites/a.png", "emotion_tags": ["微笑", "放松"]},
        {"path": "sprites/b.png", "emotion_tags": ["紧张"]},
        {"path": "sprites/c.png", "emotion_tags": []},
    ],
}
NODE = {"id": "a1", "type": "limited_turn", "maxRounds": 4, "prompt": "推进剧情"}
END_NODE = {"id": "a9", "type": "ending", "maxRounds": 0, "prompt": "收束"}
STAGE = {"stats": [{"key": "好感度", "label": "好感度", "value": 62, "max": 100},
                   {"key": "时间", "value": "第 3 天 黄昏"}],
         "bg": "bg_fog_city.jpg", "bgm": "bgm_battle.mp3"}


def cases():
    out = {}
    # build_node_prompt
    out["node_normal"] = gal.build_node_prompt(NODE, 2)
    out["node_ending"] = gal.build_node_prompt(END_NODE, 0)
    out["node_empty"] = gal.build_node_prompt(None, 0)
    # build_choice_prompt
    out["choice_with"] = gal.build_choice_prompt({"id": "a1", "transitions": ["a2", "a3"]})
    out["choice_none"] = gal.build_choice_prompt({"id": "a1", "transitions": []})
    # build_script_prompt
    out["script_min"] = gal.build_script_prompt(CHAR)
    out["script_full"] = gal.build_script_prompt(CHAR, "画像文本", 7, 4, "最近对话", "世界设定文本")
    out["script_nochar"] = gal.build_script_prompt(None, "", 3, 4, "", "")
    # build_next_act_prompt
    out["next_min"] = gal.build_next_act_prompt(CHAR)
    out["next_full"] = gal.build_next_act_prompt(CHAR, "走向X", 2, 5, "最近Y", True, "世界Z")
    # build_recap_prompt
    out["recap_min"] = gal.build_recap_prompt()
    out["recap_full"] = gal.build_recap_prompt("旧提要", "最近对话")
    # build_prompt
    out["prompt_min"] = gal.build_prompt(CHAR)
    out["prompt_full"] = gal.build_prompt(
        CHAR, True, True,
        [{"keyword": "闪光", "css": "flash", "audio": "", "mode": "after"},
         {"keyword": "震动", "css": "shake", "audio": "shake.wav", "mode": "wrap"}],
        STAGE, True)
    out["prompt_single"] = gal.build_prompt(CHAR, False, False, [], STAGE, False)
    out["prompt_nochar"] = gal.build_prompt(None, False, False, [], None, False)
    return out


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "save").lower()
    data = cases()
    if action == "save":
        with open(SNAP, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        print("已存快照：%s（%d 个用例）" % (SNAP, len(data)))
        for k, v in sorted(data.items()):
            print("   %-18s %6d 字  md5=%s" % (k, len(v), hashlib.md5(v.encode()).hexdigest()[:10]))
        return 0

    if not os.path.isfile(SNAP):
        print("找不到快照，请先跑 save")
        return 2
    with open(SNAP, "r", encoding="utf-8") as f:
        base = json.load(f)
    bad = 0
    for k in sorted(set(base) | set(data)):
        a, b = base.get(k), data.get(k)
        if a == b:
            print("   [一致] %-18s %6d 字" % (k, len(b or "")))
            continue
        bad += 1
        print("   [★差异★] %s（旧 %s 字 / 新 %s 字）" % (k, len(a or ""), len(b or "")))
        if a is not None and b is not None:
            for i in range(min(len(a), len(b))):
                if a[i] != b[i]:
                    print("        首个差异 @%d 旧=%r 新=%r" % (i, a[max(0, i - 30):i + 30],
                                                          b[max(0, i - 30):i + 30]))
                    break
    print("\n%s" % ("✓ 全部一致" if not bad else "✗ %d 个用例不一致" % bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
