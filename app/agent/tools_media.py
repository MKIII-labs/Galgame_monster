# -*- coding: utf-8 -*-

import base64
import io
import json
import os
from app import webprobe
from app import visual_qa
from app import assets
from app import audio
from app import mesh



def _t_render_check(root, args, allow_outside=False):
    """在无头浏览器里真的打开沙箱内的 HTML，回传可判定的渲染问题。

    只接受沙箱内的 .html / .htm（见 app/webprobe.py）；不联网、不安装依赖。
    screenshot=true 时额外截图交给视觉模型描述 —— 每张约 1600 token，按需再开。
    """
    from .sandbox import resolve_path
    p = resolve_path(root, args.get("path"))
    shot = str(args.get("screenshot", "")).strip().lower() in ("1", "true", "yes", "on")
    text = webprobe.check(p, viewport=args.get("viewport"), screenshot=shot)
    return text, None


def _t_visual_check(root, args, caps=None):
    """视觉 QA：错误 + WebGL 指标 + 帧率 + 截图 + 基线对比。"""
    from .sandbox import resolve_path
    p = resolve_path(root, args.get("path"))
    sid = (caps or {}).get("sid") or ""
    vps = args.get("viewports")
    if not isinstance(vps, list):
        vps = None
    text = visual_qa.check(
        p,
        viewport=str(args.get("viewport") or ""),
        viewports=vps,
        sid=sid,
        screenshots=bool(args.get("screenshot", True)),
        describe=bool(args.get("describe", False)),
        baseline=str(args.get("baseline") or ""),
        save_baseline=bool(args.get("save_baseline", False)),
    )
    return text, None


def _t_render_views(root, args, caps=None):
    """多视图截图：页面按 ?view= 设置相机。"""
    from .sandbox import resolve_path
    p = resolve_path(root, args.get("path"))
    text = visual_qa.render_views(
        p,
        views=args.get("views"),
        viewport=str(args.get("viewport") or ""),
        sid=(caps or {}).get("sid") or "",
        timeout=int(args.get("timeout") or 90),
    )
    return text, None


def _t_geometry_compare(root, args, allow_outside=False):
    """多视图几何对照：参考图与产物图按视角配对。"""
    from .sandbox import resolve_path
    from app import imagecmp
    refs = args.get("reference") or args.get("refs") or []
    cands = args.get("candidate") or args.get("candidates") or []
    if not isinstance(refs, list):
        refs = [refs]
    if not isinstance(cands, list):
        cands = [cands]
    if not refs or not cands:
        return "❌ geometry_compare 需要 reference 与 candidate 两个数组。", None
    if len(refs) != len(cands):
        return ("❌ reference（%d）与 candidate（%d）数量不一致：按视角一一配对。"
                % (len(refs), len(cands))), None
    names = args.get("names") if isinstance(args.get("names"), list) else []
    pairs = []
    for i, (r, c) in enumerate(zip(refs, cands)):
        rp = resolve_path(root, str(r or ""), allow_outside, for_read=True)
        cp = resolve_path(root, str(c or ""), allow_outside, for_read=True)
        nm = str(names[i]) if i < len(names) else "view%d" % (i + 1)
        pairs.append((nm, rp, cp))
    try:
        res = imagecmp.compare_views(
            pairs, region=args.get("region"),
            max_side=int(args.get("max_side") or imagecmp.MAX_SIDE),
            min_score=float(args.get("min_score") or 70))
    except (TypeError, ValueError) as e:
        return "❌ 对照参数不合法：%s" % e, None
    return res.get("text") or "❌ 多视图对照失败", None


def _t_scene_assert(root, args, caps=None):
    """场景断言：读页面暴露的 __VQA_SCENE_INFO__。"""
    from .sandbox import resolve_path
    p = resolve_path(root, args.get("path"))
    text = visual_qa.scene_assert(
        p, args.get("assertions") or [],
        viewport=str(args.get("viewport") or ""),
        sid=(caps or {}).get("sid") or "",
        timeout=int(args.get("timeout") or 90),
    )
    return text, None


def _t_archify(root, args, caps=None):
    """Archify：schema / doctor / finalize（原版 CLI，vendor 在 vendor/archify）。

    非 Agent 模式没有沙箱：落盘到 data/archify/<sid>/，产物地址随结果回给前端，
    由对话内嵌 iframe 渲染（不需要写入审批）。"""
    from .sandbox import (clean_sandbox, resolve_path)
    from app import archify as archify_lib
    sid = str((caps or {}).get("sid") or "")
    if not clean_sandbox(root):
        root = archify_lib.workspace_root(sid)
    act = str(args.get("action") or "finalize").strip().lower()
    if act == "doctor":
        return archify_lib.doctor(), None
    if act == "schema":
        ex = args.get("example", True)
        if isinstance(ex, str):
            ex = ex.strip().lower() not in ("false", "0", "no", "off")
        return archify_lib.schema_text(args.get("type"), bool(ex)), None
    if act != "finalize":
        return "❌ archify 的 action 只能是 finalize / schema / doctor。", None
    kind = str(args.get("type") or "").strip().lower()
    if kind not in archify_lib.DIAGRAM_TYPES:
        return ("❌ archify 需要 type（%s）" % "、".join(archify_lib.DIAGRAM_TYPES)), None
    if args.get("candidate_path"):
        cand = resolve_path(root, str(args["candidate_path"]), for_read=True)
    elif args.get("candidate"):
        raw = str(args.get("candidate") or "")
        if len(raw) > 4 * 1024 * 1024:
            return "❌ 内联候选超过 4 MB；请先 write_file 再传 candidate_path。", None
        cand = archify_lib.prepare_inline(kind, raw, root)
    else:
        return ("❌ 需要 candidate（内联内容）或 candidate_path（沙箱内候选文件）。"
                "大候选请先 write_file 再传 candidate_path。"), None
    out = str(args.get("output") or "").strip()
    output = resolve_path(root, out) if out else archify_lib.default_output(kind, root)
    parent = os.path.dirname(output)
    if parent:
        os.makedirs(parent, exist_ok=True)
    repo = ""
    if args.get("repo_root"):
        repo = resolve_path(root, str(args["repo_root"]))
    text, _rep = archify_lib.finalize(
        kind, cand, output, root,
        quality=str(args.get("quality") or "showcase"),
        repo_root=repo,
        timeout=args.get("timeout") or 0,
    )
    if (_rep or {}).get("ok"):
        # 给前端一个可直接内嵌的地址（对话里渲染成 iframe 图卡）；
        # 行首标记会被 chat.py 摘掉，不进入模型可见/落库的正文。
        rel = os.path.relpath(output, root).replace("\\", "/")
        text += "\n__ARCHIFY_ARTIFACT__ " + json.dumps({
            "type": kind,
            "path": rel,
            "url": archify_lib.view_url(sid, rel),
            "bytes": int((((_rep.get("artifact") or {}).get("bytes")) or 0)),
        }, ensure_ascii=False)
    return text, None


def _t_asset_scan(root, args, allow_outside=False):
    from .sandbox import resolve_path
    rel = str(args.get("path") or ".")
    resolve_path(root, rel)                       # 校验沙箱边界
    out = ""
    if args.get("out"):
        out = resolve_path(root, str(args["out"]))
    text, _data = assets.scan(root, rel, out=out)
    return text, None


def _t_asset_convert(root, args):
    from .sandbox import resolve_path
    from .undo import _snap
    src = resolve_path(root, args.get("from") or args.get("src") or "")
    dst = resolve_path(root, args.get("to") or args.get("dst") or "")
    undo = [_snap(dst)]
    text = assets.convert(src, dst, fmt=str(args.get("format") or ""),
                          width=int(args.get("width") or 0),
                          height=int(args.get("height") or 0),
                          quality=int(args.get("quality") or 85))
    return text, undo


def _t_asset_atlas(root, args):
    from .sandbox import resolve_path
    from .undo import _snap
    raw = args.get("images") or args.get("paths") or []
    if not isinstance(raw, list):
        raw = [raw]
    paths = [resolve_path(root, str(x)) for x in raw if str(x or "").strip()]
    out = resolve_path(root, args.get("out") or "atlas.png")
    map_out = ""
    if args.get("map"):
        map_out = resolve_path(root, str(args["map"]))
    undo = [_snap(out)] + ([_snap(map_out)] if map_out else [])
    text, _data = assets.atlas(paths, out, map_out=map_out,
                               max_size=int(args.get("max_size") or 2048),
                               padding=int(args.get("padding") or 2))
    return text, undo


def _t_asset_inline(root, args):
    from .sandbox import resolve_path
    from .undo import _snap
    raw = args.get("images") or args.get("paths") or []
    if not isinstance(raw, list):
        raw = [raw]
    paths = [resolve_path(root, str(x)) for x in raw if str(x or "").strip()]
    out = resolve_path(root, args.get("out") or "assets.js")
    undo = [_snap(out)]
    text = assets.inline(paths, out, max_kb=int(args.get("max_kb") or 0),
                         base_dir=root)
    return text, undo


def _t_asset_validate(root, args):
    from .sandbox import resolve_path
    manifest = resolve_path(root, args.get("manifest") or "assets.json")
    text, _metrics = assets.validate(manifest, base_dir=root)
    return text, None


def _t_mesh_inspect(root, args):
    """3D 模型体检：只读解析，不渲染、不改文件。"""
    from .sandbox import resolve_path
    p = resolve_path(root, args.get("path") or "")
    text, _metrics = mesh.inspect(p, units=str(args.get("units") or ""))
    return text, None


def _t_audio_probe(root, args, allow_outside=False):
    """音频体检：只读、不联网、不需要任何密钥。

    返回的是确定性测量（见 app/audio.py）；「好不好听」这类结论它给不了。
    """
    from .sandbox import resolve_path
    p = resolve_path(root, args.get("path"))
    return audio.probe(p), None


def _t_audio_compare(root, args, allow_outside=False):
    """A/B 对比：先抵消整体响度差，再比结构与包络形状。"""
    from .sandbox import resolve_path
    a = resolve_path(root, args.get("a"))
    b = resolve_path(root, args.get("b"))
    return audio.compare(a, b), None


def _b64_for_vision(path, max_side=1024):
    """读图并压到最长边 max_side，转 JPEG 后 base64（附图用，控制 token）。"""
    from .io_utils import IMG_MIME
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            if max_side and max(im.size) > max_side:
                scale = float(max_side) / float(max(im.size))
                im = im.resize((max(1, int(im.size[0] * scale)),
                                max(1, int(im.size[1] * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85, optimize=True)
        return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:                     # noqa: BLE001  PIL 失败就退回原图
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            return IMG_MIME.get(ext, "image/png"), base64.b64encode(f.read()).decode("ascii")


def _read_vision_images(root, args, allow_outside=False, max_side=0):
    """把 see_image 的参数读成 [{path, mime, b64}]；返回 (imgs, 错误文本)。"""
    from .groups import (SEE_MAX_IMAGES, SEE_MAX_TOTAL)
    from .io_utils import (IMG_EXTS, IMG_MAX_BYTES, IMG_MIME)
    from .sandbox import (_rel, resolve_path)
    raw = args.get("paths")
    paths = [str(x or "").strip() for x in (raw if isinstance(raw, list) else [])
             if str(x or "").strip()]
    single = str(args.get("path") or "").strip()
    if single:
        paths.insert(0, single)
    uniq = []
    for x in paths:
        if x not in uniq:
            uniq.append(x)
    if not uniq:
        return [], "❌ 需要 path 或 paths（沙箱内图片路径）"
    if len(uniq) > SEE_MAX_IMAGES:
        return [], "❌ 一次最多看 %d 张图，请分批" % SEE_MAX_IMAGES
    imgs, total = [], 0
    for rel in uniq:
        p = resolve_path(root, rel, allow_outside, for_read=True)
        if not os.path.isfile(p):
            return [], "❌ 文件不存在：%s" % _rel(root, p)
        ext = os.path.splitext(p)[1].lower()
        if ext not in IMG_EXTS:
            return [], "❌ 只支持图片（%s）：%s" % (" / ".join(IMG_EXTS), _rel(root, p))
        size = os.path.getsize(p)
        if size > IMG_MAX_BYTES:
            return [], ("❌ 图片太大（%s，%.1f MB，单张上限 %d MB），可先压缩再让我看"
                        % (_rel(root, p), size / 1048576.0,
                           IMG_MAX_BYTES // 1048576))
        total += size
        if total > SEE_MAX_TOTAL:
            return [], ("❌ 多图合计超过 %d MB，请减少张数或先压缩"
                        % (SEE_MAX_TOTAL // 1048576))
        if max_side:
            mime, b64 = _b64_for_vision(p, max_side)
        else:
            with open(p, "rb") as f:
                mime = IMG_MIME.get(ext, "image/png")
                b64 = base64.b64encode(f.read()).decode("ascii")
        imgs.append({"path": _rel(root, p), "mime": mime, "b64": b64})
    return imgs, ""


def vision_attachments(root, args, allow_outside=False):
    """原生视觉附图：读图并缩到 1024，供 chat 循环附到本轮上下文。"""
    return _read_vision_images(root, args, allow_outside, max_side=1024)


def _t_see_image(root, args, allow_outside=False):
    """让模型真的看一眼沙箱里的图片（走视觉通道，返回文字描述）。

    补的是这个缺口：模型能下载图片、也能写图片，但读不了像素 ——
    `read_file` 只处理文本。需要用户已配置视觉模型或聊天 API 密钥。
    paths 支持一次多张：同一请求里发给视觉通道，用于参考 vs 对象的直接对照。
    """
    imgs, err = _read_vision_images(root, args, allow_outside)
    if err:
        return err, None
    from app import imaging
    q = str(args.get("question") or "").strip()
    meta = {}
    out = imaging.recognize_image_fallback(
        imgs[0]["mime"], imgs[0]["b64"], question=q,
        images=[(it["mime"], it["b64"]) for it in imgs], meta=meta)
    if meta.get("fallback") and not str(out).startswith(("❌", "⚠️")):
        out += "\n\n（识图通道：主聊天模型未接受图片，已回落到视觉 API）"
    return out, None


def _t_image_compare(root, args, allow_outside=False):
    """对比参考图与产物图的客观差异（纯像素 / 形状统计，领域无关）。"""
    from .io_utils import IMG_EXTS
    from .sandbox import (_rel, resolve_path)
    from app import imagecmp
    raw_ref = str(args.get("reference") or args.get("ref") or "").strip()
    raw_cand = str(args.get("candidate") or args.get("cand") or "").strip()
    if not raw_ref or not raw_cand:
        return "❌ 需要 reference 与 candidate（沙箱内图片路径）", None
    ref = resolve_path(root, raw_ref, allow_outside, for_read=True)
    cand = resolve_path(root, raw_cand, allow_outside, for_read=True)
    for p in (ref, cand):
        if not os.path.isfile(p):
            return "❌ 文件不存在：%s" % _rel(root, p), None
        if os.path.splitext(p)[1].lower() not in IMG_EXTS:
            return ("❌ 只支持图片（%s）：%s"
                    % (" / ".join(IMG_EXTS), _rel(root, p))), None
    try:
        res = imagecmp.compare(ref, cand, region=args.get("region"),
                               max_side=args.get("max_side") or imagecmp.MAX_SIDE)
    except Exception as e:                # noqa: BLE001
        return "❌ 图像对照失败：%s" % e, None
    return res.get("text") or "❌ 图像对照失败", None


def _t_image_analyze(root, args, allow_outside=False):
    """测量单张图的背景 / 面板 / 比例（纯图像统计，领域无关）。"""
    from .io_utils import IMG_EXTS
    from .sandbox import (_rel, resolve_path)
    from app import imagecmp
    raw = str(args.get("path") or "").strip()
    if not raw:
        return "❌ 需要 path（沙箱内图片路径）", None
    p = resolve_path(root, raw, allow_outside, for_read=True)
    if not os.path.isfile(p):
        return "❌ 文件不存在：%s" % _rel(root, p), None
    if os.path.splitext(p)[1].lower() not in IMG_EXTS:
        return ("❌ 只支持图片（%s）：%s"
                % (" / ".join(IMG_EXTS), _rel(root, p))), None
    try:
        res = imagecmp.analyze(p, region=args.get("region"),
                               panels=args.get("panels") or "auto",
                               max_side=args.get("max_side") or imagecmp.MAX_SIDE)
    except Exception as e:                # noqa: BLE001
        return "❌ 图像分析失败：%s" % e, None
    return res.get("text") or "❌ 图像分析失败", None


def _append_blank_image_warning(root, t0, text):
    """命令结束后抽查新生成的图片：几乎全空白 / 纯色就追加一条提醒。

    只覆盖「整张图异常」（渲染没加载场景、输出路径写错、脚本失败留下空图），
    不判断内容，也不阻塞。最多列 5 张，避免把工具结果撑大。
    """
    from .base import (BLANK_RANGE_MIN, BLANK_SCAN_MAX, BLANK_STD_MIN)
    from .groups import BLOCKED_SEGMENTS
    from .io_utils import (IMG_EXTS, IMG_MAX_BYTES)
    from .sandbox import (_real, _rel)
    try:
        import numpy as np
        from PIL import Image
    except Exception:                     # noqa: BLE001
        return text
    root_real = _real(root)
    if not root_real or not os.path.isdir(root_real):
        return text
    found, seen, dirs = [], 0, 0
    try:
        for dirpath, dirnames, filenames in os.walk(root_real):
            dirs += 1
            if dirs > 2000 or seen > BLANK_SCAN_MAX:
                break
            dirnames[:] = [d for d in dirnames if d not in BLOCKED_SEGMENTS]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in IMG_EXTS:
                    continue
                seen += 1
                if seen > BLANK_SCAN_MAX:
                    break
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getmtime(p) < t0 - 2:
                        continue
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size <= 0 or size > IMG_MAX_BYTES:
                    continue
                try:
                    with Image.open(p) as im:
                        arr = np.asarray(im.convert("L").resize((64, 64)),
                                         dtype=np.float32)
                    std = float(arr.std())
                    rng = float(arr.max() - arr.min())
                except Exception:         # noqa: BLE001
                    continue
                if std < BLANK_STD_MIN or rng < BLANK_RANGE_MIN:
                    found.append((_rel(root, p), std))
    except OSError:
        return text
    if not found:
        return text
    detail = "、".join("%s（std=%.1f）" % (n, s) for n, s in found[:5])
    more = " 等 %d 张" % len(found) if len(found) > 5 else ""
    return (str(text) + "\n\n⚠️ 本轮命令生成了 %d 张几乎全空白 / 纯色的图片%s：%s"
            "\n（渲染 / 绘图脚本可能没有加载场景或输出失败；先核对再继续，不要直接交付）"
            % (len(found), more, detail))
