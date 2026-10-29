# -*- coding: utf-8 -*-
"""符号图：定义 / 引用 / 调用 / 测试映射（app/codesym.py）。"""
import os

from app import codesym


def _write(root, rel, text):
    path = os.path.join(str(root), rel.replace("/", os.sep))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_lookup_finds_definition_signature_and_doc(tmp_path):
    _write(tmp_path, "core.py",
           'def load_config(path):\n    """读配置。"""\n    return path\n')
    _write(tmp_path, "use.py",
           "from core import load_config\n\n\ndef main():\n    return load_config('x')\n")
    got = codesym.lookup(str(tmp_path), "load_config")
    assert len(got["defs"]) == 1
    rel, d = got["defs"][0]
    assert rel == "core.py"
    assert d["sig"] == "(path)"
    assert d["doc"] == "读配置。"
    # Python 的跨模块调用落在 calls，而不是 refs
    assert any(r == "use.py" for r, _ in got["calls"])


def test_impact_reports_related_tests(tmp_path):
    _write(tmp_path, "core.py", "def load_config(path):\n    return path\n")
    _write(tmp_path, "tests/test_core.py",
           "from core import load_config\n\n\ndef test_it():\n    assert load_config('x')\n")
    text = codesym.impact(str(tmp_path), "load_config")
    assert "定义 1 处" in text
    assert "tests/test_core.py" in text
    assert "python -m pytest" in text


def test_impact_file_lists_dependents_but_not_self(tmp_path):
    _write(tmp_path, "core.py", "def a():\n    return 1\n\n\ndef b():\n    return a()\n")
    _write(tmp_path, "use.py", "from core import a\n\n\ndef run():\n    return a()\n")
    text = codesym.impact(str(tmp_path), "core.py")
    assert "use.py" in text
    # 同文件内部的使用不该被列成「依赖它的文件」
    assert "core.py  (1 处)" not in text


def test_js_definition_is_indexed(tmp_path):
    _write(tmp_path, "ui.js", "export function renderPanel(el) {\n  return el\n}\n")
    got = codesym.lookup(str(tmp_path), "renderPanel")
    assert [r for r, _ in got["defs"]] == ["ui.js"]


def test_unknown_symbol_says_so_instead_of_silence(tmp_path):
    _write(tmp_path, "core.py", "x = 1\n")
    text = codesym.impact(str(tmp_path), "NoSuchThing")
    assert "没有" in text
    assert "不要据此断定它不存在" in text


def test_boost_map_prefers_definition_file(tmp_path):
    _write(tmp_path, "core.py", "def a():\n    return 1\n")
    _write(tmp_path, "use.py", "from core import a\n\n\ndef b():\n    return a()\n")
    boost = codesym.boost_map(str(tmp_path), ["a"])
    assert boost["core.py"] > boost["use.py"]


def test_broken_file_does_not_break_the_index(tmp_path):
    _write(tmp_path, "bad.py", "def f(:\n")
    _write(tmp_path, "good.py", "def g():\n    return 1\n")
    got = codesym.lookup(str(tmp_path), "g")
    assert len(got["defs"]) == 1
