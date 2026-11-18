# -*- coding: utf-8 -*-
"""把 Skill 暴露成一个只读能力：模型看索引，需要时读正文或参考文件。

只注册 skill.read 一个入口，不按 Skill 逐个注册工具——否则技能一多，
工具列表会被撑爆，也就失去了「按需加载」的意义。
"""
from app import capabilities, skills


def _names():
    return "、".join(x["name"] for x in skills.all_skills()) or "（无）"


def _read(args, ctx):
    name = str(args.get("name") or args.get("id") or "").strip()
    if not name:
        return "❌ 缺少 name。可用：%s" % _names()
    it = skills.get(name)
    if not it:
        return "❌ 没有这个 Skill：%s。可用：%s" % (name, _names())
    if not skills.enabled(it["id"]):
        return "❌ Skill 已停用：%s" % it["name"]
    file = str(args.get("file") or "").strip()
    if file:
        text = skills.read_reference(it["id"], file)
        if not text:
            files = "、".join(f["name"] for f in it["files"]) or "（这个 Skill 没有参考文件）"
            return "❌ 没有这个参考文件：%s。可用：%s" % (file, files)
        skills.record_use(it["id"])
        return "【Skill：%s】\n【参考文件：%s】\n%s" % (it["name"], file, text)
    skills.record_use(it["id"])
    return "【Skill：%s】\n%s" % (it["name"], skills.body(it["id"]))


capabilities.capability(
    "skill.read", "读取一个 Skill 的完整步骤；带 file 参数时读取 references/ 下的参考文件",
    {"name": "str", "file": "str?"}, permission="read")(_read)
