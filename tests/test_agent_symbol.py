# -*- coding: utf-8 -*-
"""轻量「LSP」：find_symbol 找定义/引用，check_code 语法体检。"""
from app import agent


def test_find_symbol_reports_definition_and_refs(tmp_path):
    (tmp_path / "a.py").write_text(
        "def ping_enemy():\n    return 1\n\n\nclass Enemy:\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from a import ping_enemy\n\n\ndef run():\n    return ping_enemy()\n", encoding="utf-8")
    text, undo = agent._t_find_symbol(str(tmp_path), {"name": "ping_enemy"})
    assert "定义 1 处" in text and "引用" in text
    assert "a.py:1" in text          # 定义行号
    assert "b.py:5" in text          # 引用行号
    assert undo is None


def test_find_symbol_class_and_missing(tmp_path):
    (tmp_path / "a.py").write_text("class Enemy:\n    pass\n", encoding="utf-8")
    text, _ = agent._t_find_symbol(str(tmp_path), {"name": "Enemy"})
    assert "class Enemy" in text
    text2, _ = agent._t_find_symbol(str(tmp_path), {"name": "NoSuchSymbol"})
    assert "没有找到符号" in text2


def test_find_symbol_requires_name(tmp_path):
    text, _ = agent._t_find_symbol(str(tmp_path), {})
    assert "请提供" in text


def test_check_code_reports_only_broken_file(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    text, _ = agent._t_check_code(str(tmp_path), {"path": "."})
    assert "2 个文件" in text and "1 个有问题" in text
    assert "bad.py" in text
    assert "ok.py" not in text


def test_check_code_clean_dir(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    text, _ = agent._t_check_code(str(tmp_path), {"path": "."})
    assert "语法体检通过" in text
