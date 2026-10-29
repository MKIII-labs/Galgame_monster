# -*- coding: utf-8 -*-
"""写后自动格式化：默认关闭、失败静默、不碰未知扩展名。"""
from app import formatter


def test_off_does_nothing(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x=1\n", encoding="utf-8")
    changed, err = formatter.format_file(str(p), "off")
    assert changed is False and err == ""
    assert p.read_text(encoding="utf-8") == "x=1\n"


def test_missing_file_is_silent(tmp_path):
    changed, err = formatter.format_file(str(tmp_path / "nope.py"), "auto")
    assert changed is False and err == ""


def test_unknown_ext_is_ignored(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x\n", encoding="utf-8")
    changed, _ = formatter.format_file(str(p), "auto")
    assert changed is False
    assert p.read_text(encoding="utf-8") == "x\n"


def test_empty_file_is_ignored(tmp_path):
    p = tmp_path / "empty.py"
    p.write_text("", encoding="utf-8")
    changed, err = formatter.format_file(str(p), "auto")
    assert changed is False and err == ""
