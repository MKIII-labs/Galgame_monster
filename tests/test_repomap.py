# -*- coding: utf-8 -*-
"""仓库结构图：Python / JS 符号提取、路径过滤、输出上限。"""
from app import repomap


def test_python_symbols(tmp_path):
    (tmp_path / "a.py").write_text(
        "import os\n\n"
        "def foo(x, y=1):\n    pass\n\n"
        "class Bar:\n    def baz(self):\n        pass\n",
        encoding="utf-8")
    txt = repomap.build(str(tmp_path))
    assert "a.py" in txt
    assert "def foo(x, y)" in txt
    assert "class Bar" in txt
    assert "def baz()" in txt


def test_js_symbols(tmp_path):
    (tmp_path / "w.js").write_text(
        "function alpha(a) { return a; }\n"
        "const beta = async (x) => x;\n"
        "class Gamma {}\n"
        "export function delta() {}\n",
        encoding="utf-8")
    txt = repomap.build(str(tmp_path))
    for name in ("alpha", "beta", "Gamma", "delta"):
        assert name in txt, name


def test_path_filter_to_single_file(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    pass\n", encoding="utf-8")
    txt = repomap.build(str(tmp_path), "a.py")
    assert "a.py" in txt and "foo" in txt
    assert "b.py" not in txt and "bar" not in txt
    assert "仓库结构" not in txt          # 单文件不输出总览


def test_limits_and_ignored_dirs(tmp_path):
    for i in range(80):
        (tmp_path / ("f%02d.py" % i)).write_text("def x%d():\n    pass\n" % i,
                                                 encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.py").write_text("def nope(): pass\n",
                                                       encoding="utf-8")
    txt = repomap.build(str(tmp_path), max_files=5)
    assert "skip.py" not in txt
    assert "未列出" in txt
    assert len(txt) <= repomap.MAX_CHARS + 200


def test_no_code_files(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    assert "没有可分析" in repomap.build(str(tmp_path))
