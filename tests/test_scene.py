# -*- coding: utf-8 -*-
"""多视图渲染 / 多视图对照 / 场景断言：参数收敛、纯逻辑与真实浏览器。"""
import json
import os

import pytest

from app import imagecmp, visual_qa, webprobe


def _img(path, color, box=None, boxes=None):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (320, 240), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for b in (boxes or ([box] if box else [(60, 40, 260, 200)])):
        d.rectangle(b, fill=color)
    im.save(str(path))
    return str(path)


def test_parse_views_limits_and_queries():
    got = visual_qa._parse_views(["front", {"name": "45度", "query": "view=iso&a=45"}])
    assert [g["name"] for g in got] == ["front", "45度"]
    assert got[0]["query"] == "view=front"
    assert got[1]["query"] == "view=iso&a=45"
    assert got[1]["file"] == "45度"


def test_compare_views_metrics(tmp_path):
    ref1 = _img(tmp_path / "r1.png", (20, 20, 20))
    ref2 = _img(tmp_path / "r2.png", (20, 20, 20), box=(40, 60, 280, 210))
    same1 = _img(tmp_path / "c1.png", (20, 20, 20))
    diff2 = _img(tmp_path / "c2.png", (20, 20, 20),
                 boxes=[(40, 110, 280, 160), (140, 60, 180, 210)])
    res = imagecmp.compare_views(
        [("front", ref1, same1), ("side", ref2, diff2)], min_score=70)
    assert res["ok"]
    assert res["metrics"]["views"] == 2
    assert res["metrics"]["views_failed"] >= 1
    assert res["metrics"]["view_score_min"] <= res["metrics"]["view_score"]
    assert "__GEOMETRY_METRICS__" in res["text"]
    assert "最差" in res["text"]


def test_compare_views_skips_unreadable(tmp_path):
    ref = _img(tmp_path / "r.png", (0, 0, 0))
    res = imagecmp.compare_views(
        [("ok", ref, ref), ("bad", str(tmp_path / "nope.png"), ref)])
    assert res["ok"] and res["metrics"]["views"] == 1
    assert "未能对照" in res["text"]


def test_scene_checks_pure():
    data = {
        "objects": [
            {"name": "Ground", "visible": True,
             "bbox": {"min": [-5, 0, -5], "max": [5, 0, 5]}},
            {"name": "Tree", "visible": True,
             "bbox": {"min": [1, 0, 1], "max": [2, 3, 2]}},
            {"name": "Hidden", "visible": False},
        ],
        "camera": {"fov": 50},
        "lights": [{"type": "DirectionalLight", "intensity": 1}],
        "renderer": {"triangles": 1200},
    }
    checks = visual_qa.scene_checks(data, [
        {"id": "g", "kind": "exists", "name": "Ground"},
        {"id": "n", "kind": "objects_min", "value": 2},
        {"id": "h", "kind": "hidden_max", "value": 1},
        {"id": "l", "kind": "lights_min", "value": 1},
        {"id": "f", "kind": "camera_fov_min", "value": 40},
        {"id": "t", "kind": "triangles_max", "value": 5000},
        {"id": "y", "kind": "bbox_y_max", "value": 3},
        {"id": "bad", "kind": "unknown_kind"},
    ])
    by = {c["id"]: c["ok"] for c in checks}
    assert by["g"] and by["n"] and by["h"] and by["l"] and by["f"] and by["t"]
    assert by["y"] and not by["bad"]
    assert checks[-1]["note"].startswith("未知检查类型")


def test_scene_checks_missing_hook_shape():
    checks = visual_qa.scene_checks({}, [{"kind": "exists", "name": "Ground"}])
    assert checks and checks[0]["ok"] is False


def test_render_views_rejects_non_html(tmp_path):
    bad = tmp_path / "a.obj"
    bad.write_text("v 0 0 0", encoding="utf-8")
    text = visual_qa.render_views(str(bad))
    assert text.startswith("❌")


@pytest.mark.skipif(not webprobe.edge_path(), reason="no Edge/Chrome")
def test_render_views_headless(tmp_path, monkeypatch):
    monkeypatch.setattr(visual_qa, "DATA_DIR", str(tmp_path))
    html = tmp_path / "views.html"
    html.write_text(
        "<!doctype html><html><head><meta charset='utf-8'></head><body>"
        "<canvas id='c' width='320' height='240'></canvas>"
        "<script>var v=new URLSearchParams(location.search).get('view')||'front';"
        "var g=document.getElementById('c').getContext('2d');"
        "g.fillStyle=(v==='front')?'#cc2222':'#2244cc';"
        "g.fillRect(0,0,320,240);</script></body></html>", encoding="utf-8")
    text = visual_qa.render_views(str(html), views=["front", "side"],
                                  viewport="480x360", sid="pytest_views")
    assert "__RENDER_VIEWS__" in text
    assert "所有视角的截图完全相同" not in text
    row = [ln for ln in text.splitlines() if ln.startswith("__RENDER_VIEWS__")][0]
    manifest = json.loads(row[len("__RENDER_VIEWS__"):].strip())
    assert len(manifest["views"]) == 2
    for v in manifest["views"]:
        assert os.path.isfile(v["screenshot"])


@pytest.mark.skipif(not webprobe.edge_path(), reason="no Edge/Chrome")
def test_scene_assert_headless(tmp_path, monkeypatch):
    monkeypatch.setattr(visual_qa, "DATA_DIR", str(tmp_path))
    html = tmp_path / "scene.html"
    html.write_text(
        "<!doctype html><html><head><meta charset='utf-8'></head><body><div>x</div>"
        "<script>window.__VQA_SCENE_INFO__=function(){return {"
        "objects:[{name:'Ground',type:'Mesh',visible:true,"
        "bbox:{min:[-5,0,-5],max:[5,0,5]}}],"
        "camera:{fov:50},lights:[{type:'DirectionalLight',intensity:1}],"
        "renderer:{triangles:1200}};};</script></body></html>", encoding="utf-8")
    text = visual_qa.scene_assert(str(html), [
        {"id": "g", "kind": "exists", "name": "Ground"},
        {"id": "l", "kind": "lights_min", "value": 1},
        {"id": "f", "kind": "camera_fov_max", "value": 60},
    ], sid="pytest_scene")
    assert "通过 3 / 3" in text
    assert "__SCENE_METRICS__" in text

    bad = visual_qa.scene_assert(str(html), [
        {"id": "t", "kind": "triangles_max", "value": 100}], sid="pytest_scene")
    assert "通过 0 / 1" in bad and "✗" in bad
