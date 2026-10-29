# -*- coding: utf-8 -*-
"""素材管线：扫描 / 转换 / 图集 / 内联 / 校验。

目标是把「素材在沙箱外、格式杂、量大」这件事从模型手工写脚本，
变成确定性工具：

- scan：索引尺寸、格式、哈希、重复、缺失；
- convert：png/webp/jpeg 转换与缩放；
- atlas：简单货架打包，生成 JSON 映射，带 padding；
- inline：base64/data URI 内联，带体积预算；
- validate：按清单检查文件是否存在、尺寸是否变化、图集是否越界。

只做本地文件处理，不联网。所有写入由调用方先做沙箱路径校验与撤销快照。
"""
import base64
import hashlib
import json
import os

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXTS = {".mp3", ".ogg", ".wav", ".flac", ".m4a"}
MODEL_EXTS = {".obj", ".gltf", ".glb", ".fbx", ".mtl"}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea"}


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return "%.1fGB" % n


def _kind(ext):
    e = str(ext or "").lower()
    if e in IMG_EXTS:
        return "image"
    if e in AUDIO_EXTS:
        return "audio"
    if e in MODEL_EXTS:
        return "model"
    return "other"


def _hash(path):
    """文件指纹：大小 + 头尾 64KB，避免大素材全量读。"""
    try:
        size = os.path.getsize(path)
        h = hashlib.sha1(str(size).encode("ascii"))
        with open(path, "rb") as f:
            h.update(f.read(65536))
            if size > 131072:
                f.seek(-65536, os.SEEK_END)
                h.update(f.read(65536))
        return h.hexdigest()[:16]
    except OSError:
        return ""


def _image_info(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return {"w": int(im.width), "h": int(im.height),
                    "mode": str(im.mode), "format": str(im.format or "")}
    except Exception as e:
        return {"error": str(e)[:120]}


def scan(root, rel=".", out=""):
    """扫描目录，返回 (给模型看的文本, 机器可读摘要)。"""
    base = os.path.abspath(root)
    target = os.path.abspath(os.path.join(base, rel or "."))
    if not (target == base or target.startswith(base + os.sep)):
        return "❌ 路径越出沙箱：%s" % rel, {}
    if not os.path.isdir(target):
        return "❌ 不是目录：%s" % (rel or "."), {}
    images, audio, models, others = [], [], [], []
    total = 0
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            total += size
            ext = os.path.splitext(fn)[1].lower()
            item = {"file": os.path.relpath(p, base).replace("\\", "/"),
                    "bytes": size, "ext": ext, "hash": _hash(p)}
            kind = _kind(ext)
            if kind == "image":
                item.update(_image_info(p))
                images.append(item)
            elif kind == "audio":
                audio.append(item)
            elif kind == "model":
                models.append(item)
            else:
                others.append(item)
    seen, dup = {}, []
    for it in images:
        key = it.get("hash")
        if not key:
            continue
        if key in seen:
            dup.append([seen[key], it["file"]])
        else:
            seen[key] = it["file"]
    data = {
        "root": os.path.relpath(target, base).replace("\\", "/"),
        "total_bytes": total,
        "images": images,
        "audio": audio,
        "models": models,
        "other_count": len(others),
        "duplicates": dup,
    }
    lines = ["【素材扫描】%s · 合计 %s" % (data["root"] or ".", _human(total)),
             "图片 %d · 音频 %d · 模型 %d · 其它 %d"
             % (len(images), len(audio), len(models), len(others))]
    bad = [it for it in images if it.get("error")]
    if bad:
        lines.append("无法读取的图片 %d 个：%s"
                     % (len(bad), "、".join(x["file"] for x in bad[:8])))
    if dup:
        lines.append("重复图片 %d 组：%s" % (len(dup), "；".join(
            " = ".join(pair) for pair in dup[:6])))
    if images:
        sizes = {}
        for it in images:
            key = "%s×%s" % (it.get("w") or "?", it.get("h") or "?")
            sizes[key] = sizes.get(key, 0) + 1
        top = sorted(sizes.items(), key=lambda kv: -kv[1])[:8]
        lines.append("图片尺寸分布：" + "、".join("%s×%d" % (k, v) for k, v in top))
    if audio:
        lines.append("音频：%s" % "、".join(x["file"] for x in audio[:8]))
    if models:
        lines.append("模型：%s" % "、".join(x["file"] for x in models[:8]))
    if out:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            lines.append("清单已写入：%s" % os.path.relpath(out, base).replace("\\", "/"))
        except OSError as e:
            lines.append("⚠️ 清单写入失败：%s" % e)
    lines.append("__ASSET_METRICS__ " + json.dumps({
        "images": len(images), "audio": len(audio), "models": len(models),
        "duplicates": len(dup), "total_bytes": total,
        "unreadable": len(bad),
    }, ensure_ascii=False))
    return "\n".join(lines), data


def convert(src, dst, fmt="", width=0, height=0, quality=85):
    """图片转换 / 缩放；返回给模型看的文本。"""
    try:
        from PIL import Image
    except Exception as e:
        return "❌ 缺少 Pillow，无法转换图片：%s" % e
    if not os.path.isfile(src):
        return "❌ 源文件不存在：%s" % src
    fmt = (fmt or os.path.splitext(dst)[1].lstrip(".") or "png").lower()
    fmt = {"jpg": "jpeg"}.get(fmt, fmt)
    if fmt not in ("png", "jpeg", "webp"):
        return "❌ 只支持 png / jpeg / webp（当前：%s）" % fmt
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGBA" if fmt in ("png", "webp") else "RGB")
            if width or height:
                w = int(width or 0) or max(1, round(im.width * (int(height) / im.height)))
                h = int(height or 0) or max(1, round(im.height * (int(width) / im.width)))
                im = im.resize((max(1, w), max(1, h)), Image.LANCZOS)
            kwargs = {}
            if fmt in ("jpeg", "webp"):
                kwargs["quality"] = max(1, min(100, int(quality or 85)))
            im.save(dst, format=fmt.upper(), **kwargs)
            out_w, out_h = im.size
        return ("【素材转换】%s → %s · %d×%d · %s"
                % (os.path.basename(src), os.path.basename(dst),
                   out_w, out_h, _human(os.path.getsize(dst))))
    except Exception as e:
        return "❌ 转换失败：%s" % e


def _load_rgba(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.convert("RGBA").copy()


def atlas(paths, out, map_out="", max_size=2048, padding=2):
    """简单货架打包；返回 (文本, 映射 dict)。"""
    try:
        from PIL import Image
    except Exception as e:
        return "❌ 缺少 Pillow，无法打包图集：%s" % e
    items = []
    for p in paths or []:
        if not os.path.isfile(p):
            return "❌ 图片不存在：%s" % p, {}
        try:
            im = _load_rgba(p)
        except Exception as e:
            return "❌ 无法读取图片 %s：%s" % (p, e), {}
        items.append({"path": p, "name": os.path.basename(p), "im": im,
                      "w": im.width, "h": im.height})
    if not items:
        return "❌ 至少给一张图片", {}
    items.sort(key=lambda x: (-x["h"], -x["w"]))
    size = max(64, int(max_size or 2048))
    pad = max(0, int(padding or 0))
    x = y = shelf_h = 0
    for it in items:
        if it["w"] + pad * 2 > size:
            return ("❌ %s 宽度 %d 超过图集上限 %d"
                    % (it["name"], it["w"], size)), {}
        if x + it["w"] + pad * 2 > size:
            x, y, shelf_h = 0, y + shelf_h, 0
        if y + it["h"] + pad * 2 > size:
            return ("❌ 图集 %d×%d 放不下（最后卡在 %s）；请增大 max_size 或减少图片"
                    % (size, size, it["name"])), {}
        it["x"], it["y"] = x + pad, y + pad
        x += it["w"] + pad * 2
        shelf_h = max(shelf_h, it["h"] + pad * 2)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for it in items:
        canvas.paste(it["im"], (it["x"], it["y"]))
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        canvas.save(out, format="PNG")
    except Exception as e:
        return "❌ 图集写入失败：%s" % e, {}
    frames = {}
    for it in items:
        base = os.path.splitext(it["name"])[0]
        key, n = base, 2
        while key in frames:                 # 不同目录同名贴图：加序号，避免互相覆盖
            key = "%s_%d" % (base, n)
            n += 1
        frames[key] = {
            "x": it["x"], "y": it["y"], "w": it["w"], "h": it["h"],
        }
    data = {"image": os.path.basename(out), "size": size,
            "padding": pad, "frames": frames}
    if map_out:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(map_out)), exist_ok=True)
            with open(map_out, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            return "⚠️ 图集已生成，但映射写入失败：%s" % e, data
    used = sum((it["w"] + pad * 2) * (it["h"] + pad * 2) for it in items)
    return ("【图集】%s · %d×%d · %d 帧 · 占用 %.0f%%%s"
            % (os.path.basename(out), size, size, len(items),
               100.0 * used / float(size * size),
               (" · 映射 " + os.path.basename(map_out)) if map_out else "")), data


def _mime(ext):
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav",
        ".flac": "audio/flac", ".m4a": "audio/mp4",
    }.get(str(ext or "").lower(), "application/octet-stream")


def inline(paths, out, max_kb=0, base_dir=""):
    """把素材转成 data URI；out 以 .js 结尾时写成 window.__ASSETS__。"""
    payload = {}
    total = 0
    for p in paths or []:
        if not os.path.isfile(p):
            return "❌ 文件不存在：%s" % p
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except OSError as e:
            return "❌ 读取失败 %s：%s" % (p, e)
        total += len(raw)
        try:
            name = (os.path.relpath(p, base_dir) if base_dir
                    else os.path.basename(p)).replace("\\", "/")
        except ValueError:
            name = os.path.basename(p)
        payload[name] = "data:%s;base64,%s" % (
            _mime(os.path.splitext(p)[1]), base64.b64encode(raw).decode("ascii"))
    limit = int(max_kb or 0) * 1024
    if limit and total > limit:
        return ("❌ 内联体积 %s 超过预算 %s；请改用复制文件或压缩后再内联"
                % (_human(total), _human(limit)))
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        if str(out).lower().endswith(".js"):
            body = "window.__ASSETS__ = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        else:
            body = json.dumps(payload, ensure_ascii=False, indent=2)
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError as e:
        return "❌ 写入失败：%s" % e
    return ("【内联】%d 个素材 · 原始 %s · 输出 %s"
            % (len(payload), _human(total), _human(os.path.getsize(out))))


def validate(manifest_path, base_dir=""):
    """按清单检查素材；返回 (文本, 指标)。"""
    if not os.path.isfile(manifest_path):
        return "❌ 清单不存在：%s" % manifest_path, {}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return "❌ 清单不是合法 JSON：%s" % e, {}
    root = base_dir or os.path.dirname(os.path.abspath(manifest_path))
    images = data.get("images") if isinstance(data, dict) else None
    # 也接受 asset_atlas 直接产出的映射（{image,size,frames}）
    if not isinstance(images, list) and isinstance(data, dict) and data.get("frames"):
        ap = str(data.get("image") or "")
        p = os.path.abspath(os.path.join(root, ap)) if ap else ""
        issues = []
        if not ap or not os.path.isfile(p):
            issues.append("图集文件不存在：%s" % (ap or "（空）"))
        size = int(data.get("size") or 0)
        for name, fr in (data.get("frames") or {}).items():
            if not isinstance(fr, dict):
                continue
            if (int(fr.get("x") or 0) + int(fr.get("w") or 0) > size
                    or int(fr.get("y") or 0) + int(fr.get("h") or 0) > size):
                issues.append("帧越界：%s" % name)
        metrics = {"images": len(data.get("frames") or {}),
                   "missing": 0 if not issues else 1,
                   "changed": 0, "unreadable": 0}
        lines = ["【素材校验】%s · 图集帧 %d" % (os.path.basename(manifest_path),
                                                metrics["images"]),
                 "图集文件：%s" % (ap or "（空）")]
        if issues:
            lines.append("问题：" + "；".join(issues[:12]))
        else:
            lines.append("图集与帧映射正常")
        lines.append("__ASSET_METRICS__ " + json.dumps(
            {"images": metrics["images"], "missing": metrics["missing"],
             "unreadable": 0, "duplicates": 0}, ensure_ascii=False))
        return "\n".join(lines), metrics
    if not isinstance(images, list):
        return "❌ 清单缺少 images 数组", {}
    missing, changed, bad = [], [], []
    for it in images:
        if not isinstance(it, dict):
            continue
        rel = str(it.get("file") or "").replace("\\", "/")
        if not rel:
            continue
        p = os.path.abspath(os.path.join(root, rel))
        if not os.path.isfile(p):
            missing.append(rel)
            continue
        try:
            from PIL import Image
            with Image.open(p) as im:
                if it.get("w") and int(it["w"]) != im.width:
                    changed.append("%s 宽 %s→%s" % (rel, it["w"], im.width))
                if it.get("h") and int(it["h"]) != im.height:
                    changed.append("%s 高 %s→%s" % (rel, it["h"], im.height))
        except Exception as e:
            bad.append("%s：%s" % (rel, str(e)[:80]))
    atlas = data.get("atlas") if isinstance(data, dict) else None
    if isinstance(atlas, dict) and atlas.get("image"):
        ap = os.path.abspath(os.path.join(root, str(atlas["image"])))
        if not os.path.isfile(ap):
            missing.append(str(atlas["image"]))
    metrics = {"images": len(images), "missing": len(missing),
               "changed": len(changed), "unreadable": len(bad)}
    lines = ["【素材校验】%s · 图片 %d" % (os.path.basename(manifest_path), len(images)),
             "缺失 %d · 尺寸变化 %d · 无法读取 %d"
             % (metrics["missing"], metrics["changed"], metrics["unreadable"])]
    if missing:
        lines.append("缺失：" + "、".join(missing[:12]))
    if changed:
        lines.append("尺寸变化：" + "；".join(changed[:12]))
    if bad:
        lines.append("无法读取：" + "；".join(bad[:12]))
    lines.append("__ASSET_METRICS__ " + json.dumps(metrics, ensure_ascii=False))
    return "\n".join(lines), metrics
