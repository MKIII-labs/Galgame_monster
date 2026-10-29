# -*- coding: utf-8 -*-
"""Skill：frontmatter 解析、增删改、索引与按需读取。"""
import os

import pytest

from app import capabilities, skills

PRESET_IDS = ["code-review", "debug-helper", "decision-compare", "doc-digest",
              "study-coach", "writing-polish",
              "spec-clarify", "verify-first", "safe-refactor", "repo-onboarding",
              "commit-pr", "data-analysis", "translate", "visual-restore"]


@pytest.fixture
def skill_dir(tmp_path, monkeypatch):
    """把 Skill 目录与配置都指到临时位置，避免污染 data/。"""
    monkeypatch.setattr(skills, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(skills, "_cache", {"sig": None, "items": []})
    cfg = {"skills_disabled": []}
    monkeypatch.setattr(skills, "load_config", lambda: cfg)
    monkeypatch.setattr(skills, "save_config", lambda c: cfg.update(c))
    return tmp_path / "skills"


def _write(sid, name, desc="说明", body="## 步骤\n1. 先做这个", enabled=True):
    text = ("---\nname: %s\ndescription: %s\nenabled: %s\n---\n\n%s\n"
            % (name, desc, "true" if enabled else "false", body))
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
        assert len(it["description"]) <= 60, sid      # L1 每个只占一行
        assert it["enabled"] is True, sid


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
