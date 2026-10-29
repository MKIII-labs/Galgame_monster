# -*- coding: utf-8 -*-
"""P1：素材管线、视觉指标、验收数值断言。"""
import json
import os


def _png(path, color=(120, 120, 120), size=(16, 16)):
    from PIL import Image
    Image.new("RGBA", size, color + (255,)).save(path, format="PNG")


def test_asset_scan_convert_atlas_inline_validate(tmp_path):
    from app import assets
    root = str(tmp_path)
    _png(os.path.join(root, "a.png"), (255, 0, 0), (16, 16))
    _png(os.path.join(root, "b.png"), (0, 255, 0), (24, 12))
    _png(os.path.join(root, "dup.png"), (255, 0, 0), (16, 16))

    text, data = assets.scan(root, ".", out=os.path.join(root, "assets.json"))
    assert data["images"] and len(data["images"]) == 3
    assert data["duplicates"]          # a.png 与 dup.png 内容相同
    assert os.path.isfile(os.path.join(root, "assets.json"))
    assert "__ASSET_METRICS__" in text

    out = os.path.join(root, "converted.webp")
    msg = assets.convert(os.path.join(root, "a.png"), out, width=8, height=8)
    assert "素材转换" in msg and os.path.isfile(out)

    atlas_out = os.path.join(root, "atlas.png")
    map_out = os.path.join(root, "atlas.json")
    atext, adata = assets.atlas(
        [os.path.join(root, "a.png"), os.path.join(root, "b.png")],
        atlas_out, map_out=map_out, max_size=128, padding=2)
    assert "图集" in atext and os.path.isfile(atlas_out)
    assert len(adata["frames"]) == 2
    with open(map_out, encoding="utf-8") as f:
        assert json.load(f)["size"] == 128

    js = os.path.join(root, "inline.js")
    itext = assets.inline([os.path.join(root, "a.png")], js)
    assert "内联" in itext and os.path.isfile(js)
    with open(js, encoding="utf-8") as f:
        assert "data:image/png;base64," in f.read()

    manifest = os.path.join(root, "bad.json")
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump({"images": [
            {"file": "a.png", "w": 16, "h": 16},
            {"file": "missing.png"},
        ]}, f)
    vtext, vmetrics = assets.validate(manifest, base_dir=root)
    assert vmetrics["missing"] == 1
    assert "缺失" in vtext


def test_visual_image_metrics_and_ssim(tmp_path):
    from PIL import Image
    import random
    from app import visual_qa

    solid = os.path.join(str(tmp_path), "solid.png")
    Image.new("RGB", (64, 64), (128, 128, 128)).save(solid)
    met = visual_qa.image_metrics(solid)
    assert met["contrast"] == 0
    assert met["blank_score"] == 1.0
    assert visual_qa.ssim(solid, solid) == 1.0

    noise = os.path.join(str(tmp_path), "noise.png")
    im = Image.new("RGB", (64, 64))
    px = im.load()
    random.seed(1)
    for x in range(64):
        for y in range(64):
            px[x, y] = (random.randrange(256), random.randrange(256), random.randrange(256))
    im.save(noise)
    nmet = visual_qa.image_metrics(noise)
    assert nmet["contrast"] > 10
    assert nmet["blank_score"] < 0.5
    assert visual_qa.ssim(solid, noise) < 0.9


def test_acceptance_numeric_expect():
    from app.api.chat import _acceptance_expect
    text = ("【视觉 QA】\n"
            '__VISUAL_METRICS__ {"fps": 60, "console_errors": 0, '
            '"sharpness": 120, "blank_score": 0.02}')
    ok, why = _acceptance_expect(
        {"fps_min": 55, "console_errors_max": 0, "blank_score_max": 0.1}, text)
    assert ok, why
    ok2, why2 = _acceptance_expect({"fps_min": 70}, text)
    assert not ok2 and "fps" in why2
    ok3, why3 = _acceptance_expect({"ssim_min": 0.9}, text)
    assert not ok3 and "ssim" in why3


def test_visual_check_headless(tmp_path, monkeypatch):
    """真实跑一次无头浏览器；没装 Edge/Chrome 时跳过。"""
    from app import visual_qa, webprobe
    if not webprobe.edge_path():
        import pytest
        pytest.skip("no Edge/Chrome")
    monkeypatch.setattr(visual_qa, "DATA_DIR", str(tmp_path))
    html = os.path.join(str(tmp_path), "game.html")
    with open(html, "w", encoding="utf-8") as f:
        f.write("<!doctype html><html><head><title>vqa</title></head><body>"
                "<canvas id='c' width='320' height='240'></canvas>"
                "<script>var c=document.getElementById('c');"
                "var g=c.getContext('2d');"
                "function f(t){g.fillStyle='#336699';g.fillRect(0,0,320,240);"
                "requestAnimationFrame(f)}requestAnimationFrame(f);</script>"
                "</body></html>")
    text = visual_qa.check(html, viewport="640x480", sid="pytest_vqa",
                           screenshots=True, baseline="smoke", save_baseline=True)
    assert "__VISUAL_METRICS__" in text
    assert "Canvas 1 个" in text
    assert "控制台错误 0" in text

