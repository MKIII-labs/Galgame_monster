# -*- coding: utf-8 -*-
"""Skill：frontmatter 解析、三层加载、触发、校验、导入导出与按需读取。"""
import io
import os
import zipfile

import pytest

from app import capabilities, skills

PRESET_IDS = ["code-review", "debug-helper", "decision-compare", "doc-digest",
              "study-coach", "writing-polish",
              "spec-clarify", "verify-first", "safe-refactor", "repo-onboarding",
              "commit-pr", "data-analysis", "translate", "visual-restore",
              "read-before-write", "asset-pipeline", "computer-use",
              "workflow-orchestrator"]


@pytest.fixture
def skill_dir(tmp_path, monkeypatch):
    """把 Skill 目录与配置都指到临时位置，避免污染 data/。"""
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(skills, "_cache", {"sig": None, "items": []})
    cfg = {"skills_disabled": []}
    monkeypatch.setattr(skills, "load_config", lambda: cfg)
    monkeypatch.setattr(skills, "save_config", lambda c: cfg.update(c))
    return tmp_path / "skills"


def _write(sid, name, desc="说明", body="## 步骤\n1. 先做这个", enabled=True, keywords=""):
    kw = ("keywords: %s\n" % keywords) if keywords else ""
    text = ("---\nname: %s\ndescription: %s\n%senabled: %s\n---\n\n%s\n"
            % (name, desc, kw, "true" if enabled else "false", body))
    err = skills.save(sid, text)
    assert err == "", err


def test_parse_frontmatter():
    meta, body = skills._parse("---\nname: 数学老师\ndescription: 指出核心错因\n---\n\n正文")
    assert meta["name"] == "数学老师"
    assert meta["description"] == "指出核心错因"
    assert body == "正文"


def test_parse_without_frontmatter():
    meta, body = skills._parse("就是一段普通文本")
    assert meta == {}
    assert body == "就是一段普通文本"


def test_empty_dir_returns_empty_index(skill_dir):
    assert skills.all_skills() == []
    assert skills.index_block() == ""


def test_save_get_body_delete(skill_dir):
    _write("math-teacher", "数学老师", "先指出最核心的错因")
    it = skills.get("math-teacher")
    assert it["name"] == "数学老师"
    assert "先做这个" in skills.body("math-teacher")
    assert skills.get("数学老师")["id"] == "math-teacher"      # 支持按名字查

    assert skills.delete("math-teacher") == ""
    assert skills.get("math-teacher") is None


def test_bad_id_rejected(skill_dir):
    assert skills.safe_id("../evil") == ""
    assert skills.safe_id("a/b") == ""
    assert skills.safe_id("") == ""
    assert skills.save("../evil", "x").startswith("❌")


def test_unique_id_never_overwrites_existing(skill_dir):
    """新建时同一 base 依次变成 -2 / -3……，不会覆盖已有的 Skill。"""
    _write("skill", "甲")
    assert skills.unique_id("skill") == "skill-2"
    _write("skill-2", "乙")
    assert skills.unique_id("skill") == "skill-3"
    assert skills.unique_id("") == "skill-3"      # 空 base 退回 skill 系列
    assert skills.unique_id("../evil") in ("skill", "skill-2", "skill-3")


def test_unique_id_respects_length_limit(skill_dir):
    base = "a" * 40
    _write(base, "长名")
    got = skills.unique_id(base)
    assert len(got) <= 40 and got.endswith("-2")
    assert skills.save(got, "---\nname: x\n---\n") == ""   # 生成出来的 id 合法可写


def test_body_is_truncated(skill_dir):
    _write("long", "长文", body="x" * (skills.BODY_LIMIT + 50))
    text = skills.body("long")
    assert "已截断" in text
    assert len(text) < skills.BODY_LIMIT + 200


def test_index_lists_enabled_only(skill_dir):
    _write("a", "甲", "第一个")
    _write("b", "乙", "第二个")
    assert "甲" in skills.index_block() and "乙" in skills.index_block()

    skills.set_enabled("b", False)
    block = skills.index_block()
    assert "甲" in block
    assert "乙" not in block
    assert skills.enabled("b") is False


def test_index_uses_schema_tool_name_and_expand_hint(skill_dir):
    """索引里的工具名必须是原生 schema 的真实名字，并限制展开数量。"""
    _write("a", "甲", "第一个")
    block = skills.index_block()
    assert "skill_read" in block
    assert "最多展开 1 个" in block


def test_cap_domains_include_skill_when_index_present():
    """Skill 索引注入时，skill_read 必须同时在 schema 里。"""
    from app import agent
    from app import skill_caps  # noqa: F401  注册 skill.read
    from app.api import chat

    doms = chat._cap_domains_with_skills(set(), True)
    assert "skill" in doms
    names = [t["function"]["name"] for t in
             agent.native_tool_schemas(agent_on=True, cap_domains=doms)]
    assert "skill_read" in names
    # 没有 Skill 时不额外挂：保持原有惰性挂载
    assert chat._cap_domains_with_skills(set(), False) == set()
    assert chat._cap_domains_with_skills(None, True) is None


def test_frontmatter_enabled_false(skill_dir):
    _write("c", "丙", enabled=False)
    assert skills.enabled("c") is False
    assert "丙" not in skills.index_block()


def test_skill_read_capability(skill_dir):
    from app import skill_caps  # noqa: F401  注册 skill.read
    _write("math-teacher", "数学老师", "先指出最核心的错因")

    assert capabilities.get("skill.read") is not None
    text, is_write = capabilities.invoke("skill.read", {"name": "数学老师"})
    assert is_write is False
    assert "【Skill：数学老师】" in text
    assert "先做这个" in text

    skills.set_enabled("math-teacher", False)
    text, _ = capabilities.invoke("skill.read", {"name": "数学老师"})
    assert text.startswith("❌")
    assert "已停用" in text


def test_skill_read_unknown_name(skill_dir):
    from app import skill_caps  # noqa: F401
    text, _ = capabilities.invoke("skill.read", {"name": "不存在"})
    assert text.startswith("❌")
    assert "没有这个 Skill" in text


def test_skill_read_reference_file(skill_dir):
    from app import skill_caps  # noqa: F401
    _write("a", "甲")
    skills.save_reference("a", "g.md", "参考内容")
    text, _ = capabilities.invoke("skill.read", {"name": "甲", "file": "g.md"})
    assert "【参考文件：g.md】" in text and "参考内容" in text
    text, _ = capabilities.invoke("skill.read", {"name": "甲", "file": "nope.md"})
    assert text.startswith("❌") and "g.md" in text
    assert skills.stats()["a"]["count"] == 1      # 读正文 / 参考文件都会计数


def test_skills_text_in_prompt():
    from app import prompt_assembly as pa
    plain, _ = pa.base_system_prompt({"scene": "general"}, "你好")
    with_skill, _ = pa.base_system_prompt({"scene": "general"}, "你好",
                                          skills_text="## 可用 Skill\n- 甲")
    assert "## 可用 Skill" not in plain          # 不传时默认提示不变
    assert "## 可用 Skill" in with_skill
    assert len(with_skill) > len(plain)


def test_presets_are_valid():
    """随项目发布的预设必须都能被解析，且默认启用。"""
    for sid in PRESET_IDS:
        path = os.path.join(skills.PRESETS_DIR, sid, skills.FILE_NAME)
        assert os.path.isfile(path), path
        it = skills._read(path, sid)
        assert it["name"], sid
        assert it["description"], sid
        assert len(it["description"]) <= 160, sid    # L1 每个只占一行
        assert "当" in it["description"], sid          # 新规范：只写触发条件
        assert it["enabled"] is True, sid


def test_presets_trigger_on_coding_request(skill_dir):
    """播种后的预设能被「改文件 + 验证」这类请求触发到。"""
    skills.seed_defaults()
    ids = [sid for sid, _name, _score in
           skills.trigger_matches("帮我修改这个文件并跑测试验证", limit=6)]
    assert "read-before-write" in ids
    assert "verify-first" in ids


def test_preset_references_are_copied(skill_dir):
    """预设带 references/ 时，播种要把参考文件一起复制（否则 L3 永远读不到）。"""
    assert skills.seed_defaults() == len(PRESET_IDS)
    it = skills.get("code-review")
    assert [f["name"] for f in it["files"]] == ["review-checklist.md"]
    assert "并发" in skills.read_reference("code-review", "review-checklist.md")


def test_reference_save_read_delete(skill_dir):
    _write("a", "甲")
    assert skills.save_reference("a", "guide.md", "# 指南\n细节") == ""
    assert [f["name"] for f in skills.get("a")["files"]] == ["guide.md"]
    assert "细节" in skills.read_reference("a", "references/guide.md")   # 带前缀也能读
    assert skills.read_reference("a", "guide.txt") == ""                # 不存在的文件
    assert skills.delete_reference("a", "guide.md") == ""
    assert skills.get("a")["files"] == []


def test_reference_rejects_path_traversal(skill_dir):
    _write("a", "甲")
    for bad in ("../x.md", "a/b.md", ".hidden.md", "x.exe", ""):
        assert skills.save_reference("a", bad, "x").startswith("❌")
        assert skills.read_reference("a", bad) == ""


def test_trigger_matches_and_hint(skill_dir):
    _write("review", "代码审查", "当要求审查代码时使用", keywords="审查, diff")
    hits = skills.trigger_matches("帮我审查这次 diff")
    assert hits and hits[0][0] == "review"
    assert "代码审查" in skills.trigger_hint("帮我审查这次 diff")
    assert skills.trigger_matches("今天天气不错") == []


def test_lint_flags_description_and_long_body(skill_dir):
    text = ("---\nname: 甲\ndescription: 先做这个再那个，步骤分三步\n---\n\n正文")
    items = skills.lint_text(text, "a")
    assert any(x["field"] == "description" and x["level"] == "warn" for x in items)

    good = ("---\nname: 甲\ndescription: 当用户要求审查时使用\nkeywords: 审查\n"
            "when_not: 纯问答\n---\n\n"
            "## 何时用\n- 审查\n\n## 不适用\n- 纯问答\n\n## 核心步骤\n1. 读\n\n"
            "## 输出格式\n- 表\n\n## 失败处理\n- 说明\n\n## 常见错误\n- 猜\n")
    assert [x for x in skills.lint_text(good, "a") if x["level"] != "ok"] == []

    long_body = "x" * (skills.BODY_LIMIT + 10)
    items = skills.lint_text(
        "---\nname: 甲\ndescription: 当用户要求审查时使用\n---\n\n" + long_body)
    assert any(x["field"] == "body" for x in items)


def test_lint_recommends_new_spec_sections(skill_dir):
    text = "---\nname: 甲\ndescription: 当用户要求审查时使用\n---\n\n正文"
    items = skills.lint_text(text, "a")
    assert any(x["field"] == "body" and "何时用" in x["message"] for x in items)
    assert any(x["field"] == "when_not" for x in items)


def test_template_contains_new_spec_sections():
    text = skills.template("测试")
    assert "when_not:" in text
    assert "## 不适用" in text and "## 失败处理" in text


def test_export_import_zip_roundtrip(skill_dir):
    _write("a", "甲")
    skills.save_reference("a", "g.md", "参考内容")
    data = skills.export_zip("a")
    sid, err = skills.import_zip(data, "imported")
    assert err == "" and sid == "imported"
    assert [f["name"] for f in skills.get(sid)["files"]] == ["g.md"]
    assert "参考内容" in skills.read_reference(sid, "g.md")


def test_import_zip_rejects_zip_slip(skill_dir):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.md", "x")
        z.writestr("SKILL.md", "---\nname: x\ndescription: 当需要时使用\n---\n")
    sid, err = skills.import_zip(buf.getvalue())
    assert sid == "" and err.startswith("❌")


def test_import_folder(skill_dir, tmp_path):
    src = tmp_path / "src"
    (src / "references").mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: 导入的技能\ndescription: 当需要导入时使用\n---\n\n正文\n",
        encoding="utf-8")
    (src / "references" / "x.md").write_text("参考", encoding="utf-8")
    sid, err = skills.import_folder(str(src))
    assert err == "" and sid
    assert [f["name"] for f in skills.get(sid)["files"]] == ["x.md"]


def test_record_use_updates_stats(skill_dir):
    _write("a", "甲")
    skills.record_use("a")
    skills.record_use("a")
    assert skills.stats()["a"]["count"] == 2


def test_preset_upgrade_overwrites_with_backup(tmp_path, monkeypatch):
    """预设规范升级：覆盖本地旧预设，但先留一份 .bak。"""
    root = tmp_path / "skills"
    presets = tmp_path / "presets" / "demo"
    presets.mkdir(parents=True)
    (presets / "SKILL.md").write_text(
        "---\nname: 新版\ndescription: 当需要新版时使用\n---\n\n新正文\n",
        encoding="utf-8")
    local = root / "demo"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text(
        "---\nname: 旧版\ndescription: 旧说明\n---\n\n旧正文\n", encoding="utf-8")
    monkeypatch.setattr(skills, "SKILLS_DIR", str(root))
    monkeypatch.setattr(skills, "PRESETS_DIR", str(tmp_path / "presets"))
    monkeypatch.setattr(skills, "_cache", {"sig": None, "items": []})
    cfg = {"skills_disabled": []}
    monkeypatch.setattr(skills, "load_config", lambda: cfg)
    monkeypatch.setattr(skills, "save_config", lambda c: cfg.update(c))

    skills.seed_defaults()
    assert skills.get("demo")["name"] == "新版"
    assert cfg["skills_preset_version"] == skills.PRESET_VERSION
    assert any(n.startswith("SKILL.md.bak-") for n in os.listdir(local))


def test_seed_defaults_copies_once(skill_dir):
    assert skills.seed_defaults() == len(PRESET_IDS)
    assert len(skills.all_skills()) == len(PRESET_IDS)
    assert skills.seed_defaults() == 0                # 第二次不再复制


def test_seed_does_not_override_user_edits(skill_dir):
    skills.seed_defaults()
    src = os.path.join(skills.SKILLS_DIR, "debug-helper", skills.FILE_NAME)
    with open(src, "w", encoding="utf-8") as f:
        f.write("---\nname: 我改过的\ndescription: 本地版本\n---\n\n正文\n")
    skills.seed_defaults()
    assert skills.get("debug-helper")["name"] == "我改过的"


def test_deleted_preset_is_not_resurrected(skill_dir):
    skills.seed_defaults()
    assert skills.delete("debug-helper") == ""
    assert skills.seed_defaults() == 0
    assert skills.get("debug-helper") is None


def test_legacy_config_seeds_new_presets_only(skill_dir, tmp_path, monkeypatch):
    """老配置（只有 skills_seeded=True）升级：新预设能进来，旧的不复活。"""
    presets = tmp_path / "presets"
    for sid in ("code-review", "spec-clarify"):
        d = presets / sid
        d.mkdir(parents=True)
        (d / skills.FILE_NAME).write_text(
            "---\nname: %s\ndescription: 说明\n---\n\n正文\n" % sid,
            encoding="utf-8")
    monkeypatch.setattr(skills, "PRESETS_DIR", str(presets))
    cfg = {"skills_seeded": True, "skills_disabled": []}
    monkeypatch.setattr(skills, "load_config", lambda: cfg)
    monkeypatch.setattr(skills, "save_config", lambda c: cfg.update(c))

    assert skills.seed_defaults() == 1          # 只复制新增的 spec-clarify
    assert skills.get("spec-clarify") is not None
    assert skills.get("code-review") is None    # 旧预设不因迁移被复活
    assert "spec-clarify" in cfg["skills_seeded_ids"]


def test_deleted_tombstone_blocks_reseed(skill_dir, monkeypatch):
    cfg = {"skills_seeded": True, "skills_disabled": [],
           "skills_deleted": ["spec-clarify"]}
    monkeypatch.setattr(skills, "load_config", lambda: cfg)
    monkeypatch.setattr(skills, "save_config", lambda c: cfg.update(c))
    skills.seed_defaults()
    assert skills.get("spec-clarify") is None


def test_api_roundtrip(skill_dir):
    from app.api import skills as api_skills
    from app.registry import get as get_route

    assert get_route("POST", "/api/skills") is not None

    class H:
        def __init__(self, body):
            self.body = body

        def _body(self):
            return self.body

        def _json(self, obj, code=200):
            return obj

    text = "---\nname: 甲\ndescription: 测试\n---\n\n正文"
    saved = api_skills._api_skills(H({"op": "save", "id": "a", "text": text}))
    assert saved["ok"] is True
    assert [x["id"] for x in saved["items"]] == ["a"]
    assert saved["items"][0]["text"].rstrip() == text.rstrip()   # 设置面板要用原文回填

    listed = api_skills._api_skills(H({"op": "list"}))
    assert listed["ok"] is True and listed["items"][0]["name"] == "甲"

    off = api_skills._api_skills(H({"op": "toggle", "id": "a", "enabled": False}))
    assert off["items"][0]["enabled"] is False

    gone = api_skills._api_skills(H({"op": "delete", "id": "a"}))
    assert gone["ok"] is True and gone["items"] == []
