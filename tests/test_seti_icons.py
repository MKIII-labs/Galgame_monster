# -*- coding: utf-8 -*-
"""IDE 文件图标：确认 Seti 字体 / 映射表已随仓库提供。"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETI = os.path.join(ROOT, "web", "vendor", "seti")


def test_seti_font_and_license_exist():
    font = os.path.join(SETI, "seti.woff")
    assert os.path.getsize(font) > 10 * 1024
    assert os.path.exists(os.path.join(SETI, "LICENSE.txt"))


def test_seti_map_has_common_types():
    with open(os.path.join(SETI, "seti-map.js"), "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"window\.GM_SETI\s*=\s*(\{.*\});", src, re.S)
    assert m
    data = json.loads(m.group(1))
    for key in ("_default", "_javascript", "_python", "_markdown", "_license"):
        assert key in data["defs"]
        # 图标必须是 Seti 字体的私用区字形，不能退化成 ASCII（曾经把 \E01E 解析成 0x01E）
        assert 0xE000 <= ord(data["defs"][key][0]) <= 0xF8FF
    assert data["dark"]["languageIds"]["javascript"] == "_javascript"
    assert data["light"]["languageIds"]["python"] == "_python_light"
