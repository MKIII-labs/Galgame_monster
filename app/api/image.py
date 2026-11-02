# -*- coding: utf-8 -*-
"""图片生成 API（由 app.registry 自注册）。

ComfyUI 是外部进程：这里只做参数校验、调用与产物登记，
不引入 torch / diffusers 依赖。默认关闭，开启见「设置 → 图片生成」。
"""
from app import image_gen, task_engine
from app.capabilities import capability
from app.registry import route


def _bad(h, msg, code=400):
    return h._json({"ok": False, "error": msg}, code)


@route("/api/image/status")
def _api_image_status(h):
    """测试连接：可带临时覆盖值，不必先保存设置。"""
    body = h._body()
    st = image_gen.status(override=body)
    out = {"ok": True}
    out.update(st)
    return h._json(out)


@route("/api/image/generate")
def _api_image_generate(h):
    body = h._body()
    try:
        out = image_gen.generate(body.get("prompt"), body.get("size"))
    except image_gen.ImageGenError as e:
        return _bad(h, str(e))
    art = task_engine.register_artifact(out["path"], name=out["filename"])
    return h._json({"ok": True, "image": out, "artifact": art})


def _cap_generate(args, ctx):
    try:
        out = image_gen.generate(args.get("prompt"), args.get("size"))
    except image_gen.ImageGenError as e:
        return "❌ 图片生成失败：%s" % e
    task_engine.register_artifact(out["path"], name=out["filename"])
    return ("已生成图片：%s（%d×%d，%s 秒），已加入工作台「产物」。"
            % (out["path"], out["width"], out["height"], out["elapsed"]))


capability("image.generate",
           "用本地 ComfyUI 生成一张图片；需要先在设置里启用图片生成",
           {"prompt": "str", "size": "square|portrait|landscape?"},
           permission="write")(_cap_generate)
