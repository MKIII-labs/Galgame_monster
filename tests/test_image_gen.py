# -*- coding: utf-8 -*-
"""本地图片生成：工作流变量替换与 ComfyUI 调用（全部 mock，不依赖 GPU）"""
import json

import pytest

from app import image_gen


def _workflow(text="__PROMPT__"):
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {"seed": 1, "steps": 30, "model": ["4", 0]},
        },
        "4": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "flux3-image-klein-int8-convrot.safetensors"},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": text, "clip": ["5", 0]},
            "_meta": {"title": "Positive Prompt"},
        },
        "7": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": 512, "height": 512, "batch_size": 1},
        },
    }


def test_build_prompt_replaces_placeholder_and_params():
    graph, seed = image_gen.build_prompt(_workflow(), "一只猫", 1024, 1216, steps=16, seed=42)
    assert graph["6"]["inputs"]["text"] == "一只猫"
    assert graph["7"]["inputs"]["width"] == 1024
    assert graph["7"]["inputs"]["height"] == 1216
    assert graph["3"]["inputs"]["steps"] == 16
    assert graph["3"]["inputs"]["seed"] == 42
    assert seed == 42
    # 原工作流不能被改动
    assert _workflow()["6"]["inputs"]["text"] == "__PROMPT__"


def test_build_prompt_falls_back_to_positive_title():
    graph, _ = image_gen.build_prompt(_workflow("原来的描述"), "新描述", 1024, 1024)
    assert graph["6"]["inputs"]["text"] == "新描述"


def test_build_prompt_requires_prompt_node():
    wf = {
        "1": {"class_type": "KSampler", "inputs": {"steps": 20}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "负向"}},
    }
    wf["3"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "另一个文本"}}
    with pytest.raises(image_gen.ImageGenError):
        image_gen.build_prompt(wf, "x", 1024, 1024)


def test_generate_requires_enabled(tmp_path):
    with pytest.raises(image_gen.ImageGenError):
        image_gen.generate("猫", cfg={"image_enabled": False})


def test_generate_missing_workflow(tmp_path):
    cfg = {
        "image_enabled": True,
        "image_workflow": str(tmp_path / "nope.json"),
        "image_dir": str(tmp_path / "out"),
    }
    with pytest.raises(image_gen.ImageGenError) as err:
        image_gen.generate("猫", cfg=cfg)
    assert "工作流" in str(err.value)


def test_generate_success(tmp_path, monkeypatch):
    wf_path = tmp_path / "wf.json"
    wf_path.write_text(json.dumps(_workflow()), encoding="utf-8")
    cfg = {
        "image_enabled": True,
        "image_workflow": str(wf_path),
        "image_dir": str(tmp_path / "out"),
        "image_comfy_url": "http://127.0.0.1:8188",
        "image_timeout": 30,
    }
    seen = {}

    def fake_post(url, data, timeout=15):
        seen["url"] = url
        seen["prompt"] = data["prompt"]
        return {"prompt_id": "p1"}

    def fake_get(url, timeout=10):
        return {
            "p1": {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {"9": {"images": [
                    {"filename": "x.png", "subfolder": "", "type": "output"}]}},
            }
        }

    def fake_request(url, data=None, timeout=30):
        seen["view"] = url
        return b"\x89PNG\r\n"

    monkeypatch.setattr(image_gen, "_POLL", 0)
    monkeypatch.setattr(image_gen, "_post_json", fake_post)
    monkeypatch.setattr(image_gen, "_get_json", fake_get)
    monkeypatch.setattr(image_gen, "_request", fake_request)

    out = image_gen.generate("一只猫", "portrait", cfg=cfg)
    assert out["width"] == 832 and out["height"] == 1216
    assert out["steps"] == image_gen.STEPS
    assert seen["prompt"]["6"]["inputs"]["text"] == "一只猫"
    assert "/view?" in seen["view"] and "filename=x.png" in seen["view"]
    assert out["path"].startswith(str(tmp_path / "out"))
    with open(out["path"], "rb") as f:
        assert f.read() == b"\x89PNG\r\n"


def test_generate_reports_comfyui_error(tmp_path, monkeypatch):
    wf_path = tmp_path / "wf.json"
    wf_path.write_text(json.dumps(_workflow()), encoding="utf-8")
    cfg = {
        "image_enabled": True,
        "image_workflow": str(wf_path),
        "image_dir": str(tmp_path / "out"),
    }
    monkeypatch.setattr(image_gen, "_POLL", 0)
    monkeypatch.setattr(image_gen, "_post_json", lambda url, data, timeout=15: {"prompt_id": "p1"})
    monkeypatch.setattr(image_gen, "_get_json", lambda url, timeout=10: {
        "p1": {"status": {"status_str": "error", "messages": [
            ["execution_error", {"exception_message": "CUDA out of memory"}]]}}
    })
    with pytest.raises(image_gen.ImageGenError) as err:
        image_gen.generate("猫", cfg=cfg)
    assert "CUDA out of memory" in str(err.value)
