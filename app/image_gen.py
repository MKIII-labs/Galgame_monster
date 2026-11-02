# -*- coding: utf-8 -*-
"""本地图片生成：通过 ComfyUI 的 HTTP API 出图并落盘。

设计取舍：
- 不引入 torch / diffusers，只用标准库 urllib：ComfyUI 是独立进程，
  本项目只当客户端，8GB 显卡的量化与卸载交给 ComfyUI 自己处理；
- 工作流不写死在代码里：官方模板是 UI 格式（含子图），必须由用户在
  ComfyUI 里「另存为 API 格式」导出一次。换模型时只换权重与工作流，
  代码不用改；
- 变量替换按「占位符优先 + 节点类型兜底」：正向提示词里保留
  ``__PROMPT__`` 最稳；没写占位符时，退而按节点标题 / 唯一文本节点猜。
"""
import copy
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from app.config import load_config
from app.store import DATA_DIR

# FLUX 3 Image Klein 是 guidance-distilled：官方推荐 16 步、无 CFG
STEPS = 16
SIZES = {
    "square": (1024, 1024),
    "portrait": (832, 1216),
    "landscape": (1216, 832),
}
_POLL = 0.6


class ImageGenError(Exception):
    """可直接展示给用户的失败原因。"""


def _base_url(cfg):
    return str(cfg.get("image_comfy_url") or "http://127.0.0.1:8188").strip().rstrip("/")


def workflow_path(cfg=None):
    cfg = cfg or load_config()
    raw = str(cfg.get("image_workflow") or "").strip()
    return raw or os.path.join(DATA_DIR, "image_workflow.json")


def output_dir(cfg=None):
    cfg = cfg or load_config()
    raw = str(cfg.get("image_dir") or "").strip()
    return raw or os.path.join(DATA_DIR, "generated")


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------
def _request(url, data=None, timeout=30):
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:                    # noqa: BLE001  错误体读不到不掩盖原始错误
            detail = ""
        raise ImageGenError("ComfyUI 返回 HTTP %s：%s" % (e.code, detail or e.reason))
    except urllib.error.URLError as e:
        raise ImageGenError("连不上 ComfyUI：%s" % (e.reason,))
    except OSError as e:
        raise ImageGenError("请求 ComfyUI 失败：%s" % e)


def _get_json(url, timeout=10):
    raw = _request(url, None, timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ImageGenError("ComfyUI 返回了非 JSON 数据（地址是否填错？）")


def _post_json(url, data, timeout=15):
    raw = _request(url, data, timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ImageGenError("ComfyUI 返回了非 JSON 数据（地址是否填错？）")


def status(cfg=None, override=None):
    """设置页「测试连接」：返回开关、地址、工作流与连通性。"""
    cfg = dict(cfg or load_config())
    if isinstance(override, dict):
        for k in ("image_comfy_url", "image_workflow"):
            if str(override.get(k) or "").strip():
                cfg[k] = override[k]
    path = workflow_path(cfg)
    out = {
        "enabled": bool(cfg.get("image_enabled")),
        "url": _base_url(cfg),
        "workflow": path,
        "workflow_exists": os.path.isfile(path),
        "reachable": False,
        "error": "",
    }
    try:
        _get_json(_base_url(cfg) + "/system_stats", timeout=3)
        out["reachable"] = True
    except ImageGenError as e:
        out["error"] = str(e)
    return out


# ------------------------------------------------------------
# 工作流
# ------------------------------------------------------------
def load_workflow(cfg=None):
    path = workflow_path(cfg)
    if not os.path.isfile(path):
        raise ImageGenError(
            "找不到图片工作流：%s。请在 ComfyUI 里打开官方 FLUX 3 Klein 模板，"
            "用「另存为 API 格式」导出到该路径（或在设置里填写路径）。" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ImageGenError("读取工作流失败：%s" % e)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def build_prompt(workflow, prompt, width, height, steps=STEPS, seed=None):
    """把提示词 / 尺寸 / 步数 / 种子写进 API 格式工作流。

    返回 ``(graph, seed)``；不修改传入的 workflow。
    """
    if not isinstance(workflow, dict) or not workflow:
        raise ImageGenError("工作流格式不对：需要 ComfyUI「另存为 API 格式」导出的 JSON")
    graph = copy.deepcopy(workflow)
    seed = int(seed if seed is not None else random.randint(1, 2 ** 31 - 1))
    used_placeholder = False
    text_nodes = []

    for node in graph.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        text = inputs.get("text")
        if isinstance(text, str):
            text_nodes.append(node)
            if "__PROMPT__" in text:
                inputs["text"] = text.replace("__PROMPT__", prompt)
                used_placeholder = True
        for key in ("width", "height"):
            if key in inputs and _is_num(inputs[key]):
                inputs[key] = width if key == "width" else height
        if "steps" in inputs and _is_num(inputs["steps"]):
            inputs["steps"] = steps
        for key in ("seed", "noise_seed"):
            if key in inputs and _is_num(inputs[key]):
                inputs[key] = seed

    if not used_placeholder:
        cand = None
        for node in text_nodes:
            title = str((node.get("_meta") or {}).get("title") or "").lower()
            if "positive" in title or "prompt" in title or "提示" in title:
                cand = node
                break
        if cand is None and len(text_nodes) == 1:
            cand = text_nodes[0]
        if cand is None:
            raise ImageGenError(
                "工作流里没找到提示词节点：请在正向提示词里保留 __PROMPT__ 占位符")
        cand["inputs"]["text"] = prompt
    return graph, seed


# ------------------------------------------------------------
# 生成
# ------------------------------------------------------------
def _wait(pid, base, timeout, started):
    while True:
        if time.time() - started > timeout:
            raise ImageGenError("等待生成超时（%d 秒）；可在设置里调大 image_timeout" % int(timeout))
        hist = _get_json(base + "/history/" + urllib.parse.quote(pid), timeout=15)
        entry = hist.get(pid) if isinstance(hist, dict) else None
        if entry:
            st = entry.get("status") or {}
            if str(st.get("status_str") or "") == "error":
                raise ImageGenError("ComfyUI 生成失败：%s" % _error_text(st))
            if entry.get("outputs") or st.get("completed"):
                return entry
        time.sleep(_POLL)


def _error_text(st):
    for msg in st.get("messages") or []:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        if str(msg[0]) != "execution_error":
            continue
        info = msg[1] if isinstance(msg[1], dict) else {}
        text = str(info.get("exception_message") or info.get("message") or "").strip()
        if text:
            return text[:300]
    return "未知错误（详见 ComfyUI 控制台）"


def _first_image(entry):
    for node_out in (entry.get("outputs") or {}).values():
        if not isinstance(node_out, dict):
            continue
        for item in node_out.get("images") or []:
            if isinstance(item, dict) and item.get("filename"):
                return item
    return None


def _save(data, cfg):
    outdir = output_dir(cfg)
    os.makedirs(outdir, exist_ok=True)
    name = "img_%s_%s.png" % (time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6])
    path = os.path.join(outdir, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def generate(prompt, size="square", steps=STEPS, timeout=None, cfg=None):
    """生成一张图并返回元信息；失败抛 ImageGenError。"""
    cfg = cfg or load_config()
    if not cfg.get("image_enabled"):
        raise ImageGenError("图片生成未启用：请到「设置 → 图片生成」打开开关")
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ImageGenError("提示词不能为空")
    width, height = SIZES.get(str(size or "").strip().lower(), SIZES["square"])
    graph, seed = build_prompt(load_workflow(cfg), prompt, width, height, steps)

    base = _base_url(cfg)
    timeout = float(timeout or cfg.get("image_timeout") or 180)
    started = time.time()
    queued = _post_json(base + "/prompt", {"prompt": graph,
                                          "client_id": uuid.uuid4().hex})
    pid = str((queued or {}).get("prompt_id") or "")
    if not pid:
        raise ImageGenError("ComfyUI 没有返回 prompt_id（工作流是否有效？）")

    entry = _wait(pid, base, timeout, started)
    img = _first_image(entry)
    if not img:
        raise ImageGenError("生成结束但没有拿到图片（工作流需要包含 SaveImage 节点）")
    query = urllib.parse.urlencode({
        "filename": img.get("filename") or "",
        "subfolder": img.get("subfolder") or "",
        "type": img.get("type") or "output",
    })
    data = _request(base + "/view?" + query, None, timeout=60)
    path = _save(data, cfg)
    return {
        "path": path,
        "filename": os.path.basename(path),
        "width": width,
        "height": height,
        "steps": steps,
        "seed": seed,
        "elapsed": round(time.time() - started, 1),
        "prompt_id": pid,
    }
