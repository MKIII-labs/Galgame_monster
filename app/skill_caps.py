# -*- coding: utf-8 -*-
"""把 Skill 暴露成一个只读能力：模型看索引，需要时读全文。

只注册 skill.read 一个入口，不按 Skill 逐个注册工具——否则技能一多，
工具列表会被撑爆，也就失去了「按需加载」的意义。
"""
from app import capabilities, skills


def _read(args, ctx):
    name = str(args.get("name") or args.get("id") or "").strip()
    if not name:
        return "❌ 缺少 name。可用：%s" % ("、".join(x["name"] for x in skills.all_skills()) or "（无）")
    it = skills.get(name)
    if not it:
        names = "、".join(x["name"] for x in skills.all_skills()) or "（当前没有启用的 Skill）"
        return "❌ 没有这个 Skill：%s。可用：%s" % (name, names)
    if not skills.enabled(it["id"]):
        return "❌ Skill 已停用：%s" % it["name"]
    return "【Skill：%s】\n%s" % (it["name"], skills.body(it["id"]))


capabilities.capability(
    "skill.read", "读取一个 Skill 的完整步骤（先看系统提示里的 Skill 索引再调用）",
    {"name": "str"}, permission="read")(_read)
