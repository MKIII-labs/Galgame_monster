# -*- coding: utf-8 -*-
"""S5：知识库导出 Markdown（frontmatter + 节点链接 + 事实清单）"""
from app.api.kb import _kb_markdown


def _kb():
    return {
        "id": "kb1", "name": "世界:设定",
        "nodes": [
            {"id": "a", "title": "甲", "content": "甲的世界", "category": "事实",
             "tags": ["规则", "地理"], "aliases": ["A"],
             "notes": [{"content": "补充了一句", "created_at": 1700000000}]},
            {"id": "b", "title": "乙", "content": "", "category": "结论"},
        ],
        "edges": [
            {"id": "e1", "from": "a", "to": "b", "relation": "cause",
             "label": "因果", "directed": True, "fact": "乙依赖甲的规则"},
            {"id": "e2", "from": "a", "to": "b", "relation": "related",
             "label": "相关", "directed": False},
            {"id": "e3", "from": "a", "to": "b", "relation": "opposite",
             "label": "对立", "directed": True, "fact": "旧结论",
             "invalid_at": 1.0},
        ],
    }


def test_frontmatter_has_title_counts_and_tags():
    md = _kb_markdown(_kb())
    assert md.startswith("---")
    assert 'title: "世界:设定"' in md
    assert "nodes: 2" in md
    assert "relations: 2" in md            # 失效边不计入
    assert 'tags: ["地理", "规则"]' in md


def test_node_section_has_meta_notes_and_links():
    md = _kb_markdown(_kb())
    assert "## 甲" in md
    assert "分类：事实" in md and "别名：A" in md
    assert "补充了一句" in md
    assert "- 因果 → [[乙]]" in md          # 单向：从甲指向乙
    assert "- 相关 ⇄ [[乙]]" in md          # 双向：两个节点各写一次


def test_facts_section_lists_valid_edges_once():
    md = _kb_markdown(_kb())
    facts = md.split("## 事实", 1)[1]
    assert facts.count("[[甲]]") == 2      # 因果 + 相关各一次
    assert "- [[甲]] --因果--> [[乙]]：乙依赖甲的规则" in facts
    assert "- [[甲]] ⇄ [[乙]]（相关）" in facts
    assert "旧结论" not in facts           # 失效边不进事实清单
    assert "对立" not in md


def test_empty_kb_has_no_facts_section():
    md = _kb_markdown({"name": "空库", "nodes": [], "edges": []})
    assert "# 空库" in md
    assert "## 事实" not in md
