# -*- coding: utf-8 -*-
"""通用还原质量：图像对照指标、工具接入、验收断言与 finish 闸门。"""
import json

from PIL import Image, ImageDraw

from app import agent, imagecmp
from app.api import chat


def _draw(path, body_w=80, color=(43, 59, 107), head=(115, 183, 229)):
    im = Image.new("RGB", (240, 360), (245, 245, 245))
    d = ImageDraw.Draw(im)
    d.ellipse((70, 20, 170, 140), fill=head)          # 头
    x0 = 120 - body_w // 2
    d.rectangle((x0, 140, x0 + body_w, 330), fill=color)   # 身体
    im.save(path)


def test_identical_images_score_full(tmp_path):
    p = tmp_path / "a.png"
    _draw(str(p))
    res = imagecmp.compare(str(p), str(p))
    assert res["ok"] is True
    m = res["metrics"]
    assert m["score"] == 100.0 and m["iou"] == 1.0 and m["palette_delta"] == 0.0
    assert "__IMAGE_METRICS__" in res["text"]


def test_different_shape_and_palette_scores_lower(tmp_path):
    a = tmp_path / "ref.png"
    b = tmp_path / "cand.png"
    _draw(str(a))
    _draw(str(b), body_w=40, color=(200, 60, 60))
    m = imagecmp.compare(str(a), str(b))["metrics"]
    assert 0 <= m["score"] < 90
    assert m["iou"] < 1.0
    assert m["palette_delta"] > 0.05
    assert m["top_gaps"]                      # 至少能给出一条差距


def test_region_and_bad_input(tmp_path):
    a = tmp_path / "a.png"
    _draw(str(a))
    res = imagecmp.compare(str(a), str(a), region=[0.1, 0.05, 0.8, 0.9])
    assert res["ok"] is True and res["metrics"]["score"] > 90
    bad = imagecmp.compare(str(a), str(a), region=[0, 0, 0, 0])
    assert bad["ok"] is False and "region" in bad["text"]
    miss = imagecmp.compare(str(tmp_path / "none.png"), str(a))
    assert miss["ok"] is False and miss["text"].startswith("❌")


def test_tool_and_acceptance_metrics(tmp_path):
    a = tmp_path / "ref.png"
    b = tmp_path / "cand.png"
    _draw(str(a))
    _draw(str(b), body_w=40, color=(200, 60, 60))
    text, undo, write = agent.run_tool(str(tmp_path), "image_compare",
                                       {"reference": "ref.png", "candidate": "cand.png"})
    assert undo is None and write is False
    assert "__IMAGE_METRICS__" in text
    metrics = chat._acceptance_metrics(text)
    assert 0 <= metrics["score"] < 90
    assert chat._acceptance_expect({"score_min": metrics["score"]}, text)[0] is True
    assert chat._acceptance_expect({"score_min": metrics["score"] + 1}, text)[0] is False
    bad, _, _ = agent.run_tool(str(tmp_path), "image_compare", {})
    assert bad.startswith("❌")


def test_fidelity_gate_action():
    g = chat._fidelity_gate_action
    assert g("nudge", False, None, 70, False, False) == "allow"
    assert g("off", True, 30, 70, False, False) == "allow"
    assert g("nudge", True, 80, 70, False, False) == "allow"
    assert g("nudge", True, 44, 70, False, False) == "nudge"
    assert g("nudge", True, 44, 70, False, True) == "allow"      # 已提醒过一次
    assert g("strict", True, 44, 70, False, True) == "block"
    assert g("strict", True, 44, 70, True, True) == "allow"      # 写了豁免理由
    assert g("nudge", True, None, 70, False, False) == "allow"   # 没有分数不拦


def test_schema_and_plan_mode_expose_image_compare():
    names = [t["function"]["name"]
             for t in agent.native_tool_schemas(agent_on=True,
                                                allow_groups={"image"})]
    assert "image" in names
    ro = [t["function"]["name"]
          for t in agent.native_tool_schemas(agent_on=True, plan_mode=True,
                                             allow_groups={"image"})]
    assert "image" in ro
    assert "edit" not in ro
    # acceptance 允许把它们当机检项
    assert "image_compare" in chat._ACCEPT_SAFE
    assert "image_analyze" in chat._ACCEPT_SAFE


def test_analyze_splits_panels_and_returns_regions(tmp_path):
    p = tmp_path / "two_panels.png"
    im = Image.new("RGB", (400, 200), (245, 245, 245))
    d = ImageDraw.Draw(im)
    d.ellipse((20, 20, 160, 180), fill=(43, 59, 107))
    d.ellipse((240, 20, 380, 180), fill=(115, 183, 229))
    im.save(p)
    res = imagecmp.analyze(str(p))
    assert res["ok"] is True
    info = res["info"]
    assert len(info["panels"]) >= 2
    first = info["panels"][0]["region"]
    assert 0 <= first[0] < 0.2 and first[2] > 0.2      # 左面板的归一化 region
    assert "ASCII 预览" in res["text"] and "__IMAGE_ANALYZE__" in res["text"]
    # region 分析后返回的坐标仍映射回原图
    left = imagecmp.analyze(str(p), region=[0, 0, 0.5, 1.0])
    assert left["ok"] is True
    assert left["info"]["bbox_region"][0] < 0.2
    # 显式网格最稳：2x2 → 4 个等分面板
    grid = imagecmp.analyze(str(p), panels="2x2")
    assert len(grid["info"]["panels"]) == 4
    assert grid["info"]["panels"][0]["region"] == [0.0, 0.0, 0.5, 0.5]
    assert imagecmp.analyze(str(p), panels="none")["info"]["panels"] == []


def test_image_analyze_tool(tmp_path):
    p = tmp_path / "a.png"
    _draw(str(p))
    text, undo, write = agent.run_tool(str(tmp_path), "image_analyze",
                                       {"path": "a.png"})
    assert undo is None and write is False
    assert "__IMAGE_ANALYZE__" in text and "宽高比" in text
    bad, _, _ = agent.run_tool(str(tmp_path), "image_analyze", {})
    assert bad.startswith("❌")


def test_blank_image_warning(tmp_path):
    blank = tmp_path / "blank.png"
    Image.new("RGB", (64, 64), (255, 255, 255)).save(blank)
    import time
    t0 = time.time() - 1
    out = agent._append_blank_image_warning(str(tmp_path), t0, "命令完成")
    assert "全空白" in out and "blank.png" in out

    normal = tmp_path / "normal.png"
    _draw(str(normal))
    out2 = agent._append_blank_image_warning(str(tmp_path), time.time() - 1, "ok")
    # 正常图不该被误报（blank.png 仍在，所以只检查 normal 没被点名）
    assert "normal.png" not in out2


def test_see_image_multi_capture(monkeypatch, tmp_path):
    from app import imaging
    for name in ("a.png", "b.png"):
        _draw(str(tmp_path / name))
    seen = {}

    def fake(mime, b64, question="", prefer=None, images=None, meta=None):
        seen["images"] = list(images or [])
        seen["q"] = question
        if meta is not None:
            meta.update({"provider": "vision", "first": "chat", "fallback": True})
        return "描述"

    monkeypatch.setattr(imaging, "recognize_image_fallback", fake)
    text, undo, write = agent.run_tool(
        str(tmp_path), "see_image",
        {"paths": ["a.png", "b.png"], "question": "比较两张"})
    assert len(seen["images"]) == 2 and seen["q"] == "比较两张"
    assert "回落" in text and undo is None and write is False


def test_fidelity_stale_next():
    f = chat._fidelity_stale_next
    assert f([50, 55], 0) == 0                     # 次数不够
    assert f([50, 55, 56], 0) == 1                 # 比此前最佳高 1 → 仍算没明显提升
    assert f([50, 55, 60], 0) == 0                 # 明显提升，清零
    assert f([50, 55, 55], 1) == 2                 # 连续第二次没提升
    assert f([50, 55, 40], 1) == 2                 # 变差也算没提升
