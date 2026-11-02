# -*- coding: utf-8 -*-
"""图像对照：把「像不像」变成可断言的客观指标（领域无关）。

只做像素 / 形状统计，不理解语义、不判断审美：
- 轮廓重合 IoU（主体剪影，按包围盒归一化后比较）
- 宽高比差、前景占比差
- 结构相似度 SSIM（灰度、主体区域）
- 主色分布差（前景像素量化后的分布距离）
- 边缘密度差、3×3 局部网格差
- 加权总分 0–100

用途：给「参考物 → 产物」这类还原任务一个可机检的对照基线，
配合 acceptance 的数值断言与质量闸门使用。典型场景（界面截图、插画、
3D 渲染、图表、排版）都由调用方自己提供两张图和验收阈值，
本模块不认识任何具体领域。

限制：
- 背景尽量干净；有地面 / 阴影 / 大块渐变背景时，请用 region 裁到主体。
- 不理解语义：颜色对了但结构错了也可能得高分，反之亦然；
  它只是把「差异」量出来，最终好不好看仍需要人 / 视觉模型判断。
"""
import json
import re

MAX_SIDE = 1024
GRID = 3
TOP_COLORS = 8


def _clip_region(region):
    """归一化区域 [x, y, w, h]（0–1）→ 合法元组；非法返回 None。"""
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return None
    try:
        x, y, w, h = [float(v) for v in region]
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    w = min(max(w, 0.0), 1.0 - x)
    h = min(max(h, 0.0), 1.0 - y)
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def _load(path, region=None, max_side=MAX_SIDE):
    """读图 → (RGB float 数组, 前景 mask, (w,h))。失败抛 ValueError。"""
    try:
        import numpy as np
        from PIL import Image
    except Exception as e:                # noqa: BLE001
        raise ValueError("需要 Pillow 与 numpy：%s" % e)
    try:
        im = Image.open(path)
        im.load()
    except Exception as e:                # noqa: BLE001
        raise ValueError("读不了图片 %s：%s" % (path, e))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    w, h = im.size
    if region:
        x, y, rw, rh = region
        box = (int(round(x * w)), int(round(y * h)),
               int(round((x + rw) * w)), int(round((y + rh) * h)))
        if box[2] > box[0] and box[3] > box[1]:
            im = im.crop(box)
    if max_side and max(im.size) > max_side:
        scale = float(max_side) / float(max(im.size))
        im = im.resize((max(1, int(im.size[0] * scale)),
                        max(1, int(im.size[1] * scale))), Image.BILINEAR)
    arr = np.asarray(im.convert("RGBA"), dtype=np.uint8)
    alpha = arr[..., 3]
    bg = None
    if (alpha < 250).any():               # 带透明通道：透明即背景
        mask = alpha > 16
    else:
        rgb = arr[..., :3].astype(np.float32)
        # 背景取「出现最多的颜色」而不是边框中位数：参考图常有边框线 / 标签，
        # 中位数会被这些少量像素带偏，导致整张图都被当成前景。
        px = rgb.reshape(-1, 3)
        q = (px // 8).astype(np.int32)
        keys = q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]
        counts = np.bincount(keys, minlength=32768)
        top = int(counts.argmax())
        bg_arr = px[keys == top].mean(axis=0)
        mask = np.abs(rgb - bg_arr).sum(axis=2) > 40
        bg = [int(round(float(v))) for v in bg_arr]
    if not mask.any():
        raise ValueError("前景为空：背景可能和主体同色，或 region 裁错了")
    return arr[..., :3].astype(np.float32), mask, (w, h), bg


def _largest_box(mask, work=256):
    """前景里最大连通块的包围盒（原图坐标）。

    先缩到 work 边长做 4 邻域 BFS，避免把边框线、图注文字、碎屑算进主体。
    """
    import numpy as np
    from PIL import Image
    h, w = mask.shape
    scale = float(work) / float(max(h, w)) if max(h, w) > work else 1.0
    small = mask
    if scale < 1.0:
        small = np.asarray(Image.fromarray((mask * 255).astype(np.uint8)).resize(
            (max(1, int(w * scale)), max(1, int(h * scale))), Image.NEAREST)) > 127
    sh, sw = small.shape
    seen = np.zeros((sh, sw), dtype=bool)
    best = None
    best_n = 0
    stack = []
    for y in range(sh):
        row = small[y]
        for x in np.nonzero(row)[0]:
            x = int(x)
            if seen[y, x]:
                continue
            seen[y, x] = True
            stack = [(y, x)]
            n = 0
            x0, x1, y0, y1 = x, x, y, y
            while stack:
                cy, cx = stack.pop()
                n += 1
                x0 = min(x0, cx)
                x1 = max(x1, cx)
                y0 = min(y0, cy)
                y1 = max(y1, cy)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < sh and 0 <= nx < sw and small[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if n > best_n:
                best_n = n
                best = (x0, y0, x1, y1)
    if best is None:
        return None
    x0, y0, x1, y1 = best
    if scale < 1.0:
        inv = 1.0 / scale
        x0, y0, x1, y1 = [int(round(v * inv)) for v in (x0, y0, x1, y1)]
    x0 = max(0, min(x0, w - 1))
    x1 = max(0, min(x1, w - 1))
    y0 = max(0, min(y0, h - 1))
    y1 = max(0, min(y1, h - 1))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _norm_mask(mask, box, size=256):
    """按包围盒裁出 mask 并缩放到 size×size（最近邻，保持形状）。"""
    import numpy as np
    from PIL import Image
    x0, y0, x1, y1 = box
    crop = (mask[y0:y1 + 1, x0:x1 + 1].astype(np.uint8) * 255)
    im = Image.fromarray(crop).resize((size, size), Image.NEAREST)
    return np.asarray(im) > 127


def _gray(arr, box, size=256):
    import numpy as np
    from PIL import Image
    x0, y0, x1, y1 = box
    crop = arr[y0:y1 + 1, x0:x1 + 1].astype(np.uint8)
    im = Image.fromarray(crop).convert("L").resize((size, size), Image.BILINEAR)
    return np.asarray(im, dtype=np.float32)


def _ssim(a, b):
    """全局 SSIM（单窗口），与 visual_qa 同一口径。"""
    import numpy as np
    mu_a, mu_b = float(a.mean()), float(b.mean())
    va, vb = float(a.var()), float(b.var())
    cov = float(((a - mu_a) * (b - mu_b)).mean())
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    den = (mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2)
    if den <= 0:
        return None
    return max(-1.0, min(1.0, float(((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / den)))


def _palette(arr, mask, top=TOP_COLORS):
    """前景主色分布：RGB 各取高 3 位（8³ 桶），返回 {桶: 占比}。"""
    import numpy as np
    px = arr[mask]
    if px.size == 0:
        return {}
    q = (px // 32).astype(np.int32)
    keys = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
    counts = np.bincount(keys, minlength=512)
    total = int(counts.sum()) or 1
    idx = np.argsort(counts)[-top:]
    return {int(k): float(counts[k]) / total for k in idx if counts[k]}


def _palette_delta(pa, pb):
    keys = set(pa) | set(pb)
    return 0.5 * sum(abs(pa.get(k, 0.0) - pb.get(k, 0.0)) for k in keys)


def _edge_density(gray):
    import numpy as np
    gx = float(np.abs(np.diff(gray, axis=1)).mean()) if gray.shape[1] > 1 else 0.0
    gy = float(np.abs(np.diff(gray, axis=0)).mean()) if gray.shape[0] > 1 else 0.0
    return (gx + gy) / 2.0 / 255.0


def _grid_delta(ma, mb, grid=GRID):
    """把两张归一化 mask 切 grid×grid，返回 (每格前景占比差的列表, 最大差)。"""
    import numpy as np
    rows = []
    h, w = ma.shape
    for gy in range(grid):
        for gx in range(grid):
            ys, ye = int(h * gy / grid), int(h * (gy + 1) / grid)
            xs, xe = int(w * gx / grid), int(w * (gx + 1) / grid)
            a = float(ma[ys:ye, xs:xe].mean())
            b = float(mb[ys:ye, xs:xe].mean())
            rows.append(abs(a - b))
    return rows, (max(rows) if rows else 0.0)


def _bbox_all(mask):
    ys, xs = mask.any(axis=1), mask.any(axis=0)
    if not xs.any() or not ys.any():
        return None
    x0, x1 = int(xs.argmax()), int(len(xs) - 1 - xs[::-1].argmax())
    y0, y1 = int(ys.argmax()), int(len(ys) - 1 - ys[::-1].argmax())
    return x0, y0, x1, y1


def _close_gaps(has, min_gap):
    """把宽度 < min_gap 的空隙合并掉，避免把主体内部的小缝切成面板。"""
    import numpy as np
    out = has.copy()
    n = len(has)
    i = 0
    while i < n:
        if has[i]:
            i += 1
            continue
        j = i
        while j < n and not has[j]:
            j += 1
        if i > 0 and j < n and (j - i) < max(1, min_gap):
            out[i:j] = True
        i = j
    return out


def _segments(has):
    segs, start = [], None
    for i, v in enumerate(has):
        if v and start is None:
            start = i
        elif not v and start is not None:
            segs.append((start, i - 1))
            start = None
    if start is not None:
        segs.append((start, len(has) - 1))
    return segs


def _axis_has(sub, axis, min_cov=0.01):
    """按覆盖率判断这一列 / 行有没有内容。

    不能用「有没有像素」：参考图的外框和分隔线只占 1–2 像素，
    却会让每一列都有像素，导致永远切不开面板。1% 覆盖率能滤掉细线，
    同时保住主体边缘（主体列通常远高于 1%）。
    """
    return sub.mean(axis=axis) > min_cov


def _split_panels(mask, min_gap_ratio=0.02, min_area_ratio=0.004,
                  max_panels=12, depth=0):
    """按整行 / 整列的低覆盖背景把图切成面板（启发式，仅供对照参考）。"""
    box = _bbox_all(mask)
    if not box:
        return []
    x0, y0, x1, y1 = box
    rects = []

    def split(rect, d):
        if len(rects) >= max_panels:
            return
        rx0, ry0, rx1, ry1 = rect
        sub = mask[ry0:ry1 + 1, rx0:rx1 + 1]
        if sub.size == 0:
            return
        cols = _close_gaps(_axis_has(sub, 0), int((rx1 - rx0 + 1) * min_gap_ratio))
        segs = _segments(cols)
        if len(segs) > 1 and d < 4:
            for a, b in segs:
                split((rx0 + a, ry0, rx0 + b, ry1), d + 1)
            return
        rows = _close_gaps(_axis_has(sub, 1), int((ry1 - ry0 + 1) * min_gap_ratio))
        segs = _segments(rows)
        if len(segs) > 1 and d < 4:
            for a, b in segs:
                split((rx0, ry0 + a, rx1, ry0 + b), d + 1)
            return
        if (rx1 - rx0 + 1) * (ry1 - ry0 + 1) < min_area_ratio * mask.size:
            return
        rects.append(rect)

    split((x0, y0, x1, y1), depth)
    return rects


def _row_col_profile(mask, buckets=12):
    """行 / 列前景占比（0–100，从上到下 / 从左到右）。"""
    h, w = mask.shape
    rows = []
    for i in range(buckets):
        a, b = int(h * i / buckets), max(int(h * (i + 1) / buckets), int(h * i / buckets) + 1)
        rows.append(int(round(float(mask[a:b].mean()) * 100)))
    cols = []
    for i in range(buckets):
        a, b = int(w * i / buckets), max(int(w * (i + 1) / buckets), int(w * i / buckets) + 1)
        cols.append(int(round(float(mask[:, a:b].mean()) * 100)))
    return rows, cols


def _ascii_mask(mask, cols=44, rows=20):
    import numpy as np
    from PIL import Image
    im = Image.fromarray((mask.astype(np.uint8) * 255)).resize((cols, rows), Image.BILINEAR)
    a = np.asarray(im)
    return "\n".join("".join("#" if v >= 40 else "." for v in row) for row in a)


def _hex_of_bin(key):
    r = ((key // 64) % 8) * 32 + 16
    g = ((key // 8) % 8) * 32 + 16
    b = (key % 8) * 32 + 16
    return "#%02X%02X%02X" % (r, g, b)


def analyze(path, region=None, panels="auto", max_side=MAX_SIDE):
    """测量一张图：背景、前景占比、bbox、宽高比、主色、行/列剖面、面板切分。

    只做图像统计，不理解内容；返回的 region 是**原图归一化坐标**，
    可以直接交给 image_compare(region=...) 逐面板对照。
    """
    reg = _clip_region(region)
    if region is not None and reg is None:
        return {"ok": False, "text": "❌ region 需要 [x, y, w, h]（0–1 的归一化坐标）",
                "info": {}, "error": "bad_region"}
    try:
        rgb, mask, size, bg = _load(path, reg, max_side)
    except ValueError as e:
        return {"ok": False, "text": "❌ %s" % e, "info": {}, "error": str(e)}
    h, w = mask.shape
    box = _bbox_all(mask)
    if not box:
        return {"ok": False, "text": "❌ 没有前景像素（检查背景 / region）",
                "info": {}, "error": "empty_mask"}
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    fg_ratio = float(mask.mean())
    aspect = float(bw) / float(bh or 1)
    fill = float(mask[y0:y1 + 1, x0:x1 + 1].mean())

    # 归一化坐标 → 原图坐标（region 先裁剪过就映射回去）
    def to_orig(nx0, ny0, nx1, ny1):
        if not reg:
            return [round(nx0, 4), round(ny0, 4), round(nx1 - nx0, 4), round(ny1 - ny0, 4)]
        rx, ry, rw, rh = reg
        return [round(rx + nx0 * rw, 4), round(ry + ny0 * rh, 4),
                round((nx1 - nx0) * rw, 4), round((ny1 - ny0) * rh, 4)]

    bbox_region = to_orig(x0 / w, y0 / h, (x1 + 1) / w, (y1 + 1) / h)

    palette = _palette(rgb, mask, top=6)
    pal_rows = sorted(palette.items(), key=lambda kv: kv[1], reverse=True)
    pal_txt = " · ".join("%s %.0f%%" % (_hex_of_bin(k), v * 100)
                         for k, v in pal_rows) or "（无）"

    rows_prof, cols_prof = _row_col_profile(mask)
    panel_rows = []
    mode = str(panels or "auto").strip().lower()
    grid = re.match(r"^(\d+)\s*x\s*(\d+)$", mode)
    if grid:
        # 显式网格：多视图参考图最稳的切法（2x2 / 1x3 …）
        gc = max(1, min(int(grid.group(1)), 8))
        gr = max(1, min(int(grid.group(2)), 8))
        for gj in range(gr):
            for gi in range(gc):
                nx0, nx1 = gi / float(gc), (gi + 1) / float(gc)
                ny0, ny1 = gj / float(gr), (gj + 1) / float(gr)
                px0, px1 = int(nx0 * w), max(int(nx1 * w) - 1, int(nx0 * w))
                py0, py1 = int(ny0 * h), max(int(ny1 * h) - 1, int(ny0 * h))
                sub = mask[py0:py1 + 1, px0:px1 + 1]
                panel_rows.append({
                    "n": len(panel_rows) + 1,
                    "region": to_orig(nx0, ny0, nx1, ny1),
                    "aspect": round((nx1 - nx0) * w / max(1.0, (ny1 - ny0) * h), 3),
                    "fg": round(float(sub.mean()), 3) if sub.size else 0.0,
                    "px": [int(px0), int(py0), int(px1), int(py1)],
                })
    elif mode not in ("none", "off", "0", "false"):
        for (px0, py0, px1, py1) in _split_panels(mask):
            pw, ph = px1 - px0 + 1, py1 - py0 + 1
            # 细长条（分隔线 / 图注行）不算面板，否则会出现 aspect 26 这种噪声
            if pw < 0.03 * w or ph < 0.03 * h:
                continue
            panel_rows.append({
                "n": len(panel_rows) + 1,
                "region": to_orig(px0 / w, py0 / h, (px1 + 1) / w, (py1 + 1) / h),
                "aspect": round(float(pw) / float(ph or 1), 3),
                "fg": round(float(mask[py0:py1 + 1, px0:px1 + 1].mean()), 3),
                "px": [int(px0), int(py0), int(px1), int(py1)],
            })

    info = {
        "size": list(size), "bg": bg, "fg_ratio": round(fg_ratio, 4),
        "bbox_region": bbox_region, "aspect": round(aspect, 3),
        "fill": round(fill, 3), "palette": [
            {"hex": _hex_of_bin(k), "pct": round(v * 100, 1)} for k, v in pal_rows],
        "row_profile": rows_prof, "col_profile": cols_prof,
        "panels": [{k: p[k] for k in ("n", "region", "aspect", "fg")}
                   for p in panel_rows],
    }
    bg_txt = ("透明" if bg is None else
              "#%02X%02X%02X" % tuple(bg))
    lines = [
        "图像分析：%s" % path,
        "尺寸 %d×%d · 背景 %s · 前景占比 %.1f%%"
        % (size[0], size[1], bg_txt, fg_ratio * 100),
        "主体 bbox（归一化）%s · 宽高比 %.2f · 内部填充 %.2f"
        % (bbox_region, aspect, fill),
        "主色（前 6）：" + pal_txt,
    ]
    if panel_rows:
        how = ("网格 %s" % mode) if grid else "自动切分（启发式）"
        lines.append("面板 %d 个（%s）：" % (len(panel_rows), how))
        for p in panel_rows:
            lines.append("  %d. region %s · 宽高比 %.2f · 前景 %.2f"
                         % (p["n"], p["region"], p["aspect"], p["fg"]))
        if not grid:
            lines.append("（自动切分可能把图注 / 分隔线算进来；多视图建议直接给 "
                         "panels=\"2x2\" / \"1x3\"，或用 region 手动指定）")
    lines.append("行剖面（上→下，前景%%）：" + ",".join(str(v) for v in rows_prof))
    lines.append("列剖面（左→右，前景%%）：" + ",".join(str(v) for v in cols_prof))
    lines.append("ASCII 预览（#=前景）：")
    lines.append(_ascii_mask(mask))
    lines.append("__IMAGE_ANALYZE__ " + json.dumps(info, ensure_ascii=False))
    return {"ok": True, "text": "\n".join(lines), "info": info, "error": ""}


def compare(ref_path, cand_path, region=None, max_side=MAX_SIDE):
    """对照两张图。返回 {ok, text, metrics, error}；失败时 ok=False。"""
    reg = _clip_region(region)
    if region is not None and reg is None:
        return {"ok": False, "text": "❌ region 需要 [x, y, w, h]（0–1 的归一化坐标）",
                "metrics": {}, "error": "bad_region"}
    try:
        a_rgb, a_mask, a_size, _ = _load(ref_path, reg, max_side)
        b_rgb, b_mask, b_size, _ = _load(cand_path, reg, max_side)
    except ValueError as e:
        return {"ok": False, "text": "❌ %s" % e, "metrics": {}, "error": str(e)}
    box_a, box_b = _largest_box(a_mask), _largest_box(b_mask)
    if not box_a or not box_b:
        return {"ok": False, "text": "❌ 有一侧没有前景像素（检查背景 / region）",
                "metrics": {}, "error": "empty_mask"}

    ma = _norm_mask(a_mask, box_a)
    mb = _norm_mask(b_mask, box_b)
    inter = (ma & mb).sum()
    union = (ma | mb).sum()
    iou = float(inter) / float(union or 1)
    fill_a, fill_b = float(ma.mean()), float(mb.mean())
    fill_delta = abs(fill_a - fill_b)

    aw = box_a[2] - box_a[0] + 1
    ah = box_a[3] - box_a[1] + 1
    bw = box_b[2] - box_b[0] + 1
    bh = box_b[3] - box_b[1] + 1
    aspect_a = float(aw) / float(ah or 1)
    aspect_b = float(bw) / float(bh or 1)
    aspect_delta = abs(aspect_a - aspect_b) / max(aspect_a, aspect_b, 1e-6)

    gray_a, gray_b = _gray(a_rgb, box_a), _gray(b_rgb, box_b)
    ssim = _ssim(gray_a, gray_b)
    edge_a, edge_b = _edge_density(gray_a), _edge_density(gray_b)
    edge_delta = abs(edge_a - edge_b)

    pa, pb = _palette(a_rgb, a_mask), _palette(b_rgb, b_mask)
    palette_delta = _palette_delta(pa, pb)

    cells, grid_max = _grid_delta(ma, mb)

    s_iou = max(0.0, min(1.0, iou))
    s_ssim = max(0.0, min(1.0, ssim if ssim is not None else 0.0))
    s_pal = max(0.0, 1.0 - min(1.0, palette_delta))
    s_asp = max(0.0, 1.0 - min(1.0, aspect_delta / 0.6))
    s_edge = max(0.0, 1.0 - min(1.0, edge_delta / 0.08))
    s_grid = max(0.0, 1.0 - min(1.0, grid_max))
    score = round(100.0 * (0.30 * s_iou + 0.18 * s_ssim + 0.14 * s_pal
                           + 0.10 * s_asp + 0.08 * s_edge + 0.20 * s_grid), 1)

    # 差距清单：只写指标与建议阈值，不含任何领域名词
    gaps = []
    if iou < 0.75:
        gaps.append((1.0 - iou, "轮廓重合：IoU %.2f（建议 ≥0.75）" % iou))
    if aspect_delta > 0.15:
        gaps.append((aspect_delta, "宽高比：差 %.0f%%（建议 ≤15%%）"
                     % (aspect_delta * 100)))
    if ssim is not None and ssim < 0.75:
        gaps.append((0.75 - ssim, "结构相似：SSIM %.2f（建议 ≥0.75）" % ssim))
    if palette_delta > 0.20:
        gaps.append((palette_delta, "主色分布：差 %.0f%%（建议 ≤20%%）"
                     % (palette_delta * 100)))
    if edge_delta > 0.02:
        gaps.append((edge_delta / 0.08, "边缘密度：差 %.3f（细节量不一致）"
                     % edge_delta))
    if grid_max > 0.25:
        gaps.append((grid_max, "局部区域：最大格差 %.0f%%（某一块明显不同）"
                     % (grid_max * 100)))
    gaps.sort(key=lambda x: x[0], reverse=True)
    top_gaps = [g[1] for g in gaps[:3]]

    metrics = {
        "score": score, "iou": round(iou, 4),
        "aspect_delta": round(aspect_delta, 4),
        "ssim": round(ssim, 4) if ssim is not None else None,
        "palette_delta": round(palette_delta, 4),
        "edge_delta": round(edge_delta, 4),
        "fill_delta": round(fill_delta, 4),
        "grid_max": round(grid_max, 4),
        "aspect_ref": round(aspect_a, 3), "aspect_cand": round(aspect_b, 3),
        "size_ref": list(a_size), "size_cand": list(b_size),
        "top_gaps": top_gaps,
    }
    lines = [
        "图像对照：%s ↔ %s" % (ref_path, cand_path),
        "总分 %.1f/100 · 轮廓 IoU %.2f · 宽高比差 %.0f%% · SSIM %s · "
        "主色差 %.0f%% · 局部最大格差 %.0f%%"
        % (score, iou, aspect_delta * 100,
           ("%.2f" % ssim) if ssim is not None else "—",
           palette_delta * 100, grid_max * 100),
        "主体宽高比：参考 %.2f / 产物 %.2f；前景占比：参考 %.2f / 产物 %.2f"
        % (aspect_a, aspect_b, fill_a, fill_b),
    ]
    if top_gaps:
        lines.append("差距（按影响排序）：")
        lines += ["%d. %s" % (i + 1, g) for i, g in enumerate(top_gaps)]
    else:
        lines.append("未发现超过建议阈值的差异。")
    lines.append("（这是像素 / 形状统计，不理解语义，也不判断好不好看）")
    lines.append("__IMAGE_METRICS__ " + json.dumps(metrics, ensure_ascii=False))
    return {"ok": True, "text": "\n".join(lines), "metrics": metrics, "error": ""}


def compare_views(pairs, region=None, max_side=MAX_SIDE, min_score=70):
    """多视图对照：pairs = [(name, ref_path, cand_path), ...]。

    每个视图复用单图 compare 的指标（轮廓 IoU / SSIM / 主色 / 局部差），
    再给整体与最差视图。视角不同、单视图看不出比例问题时用它。
    """
    rows, skipped = [], []
    for item in pairs or []:
        if not (isinstance(item, (list, tuple)) and len(item) == 3):
            continue
        name, ref, cand = [str(x or "") for x in item]
        name = name or "view%d" % (len(rows) + 1)
        res = compare(ref, cand, region=region, max_side=max_side)
        if not res.get("ok"):
            skipped.append("%s（%s）" % (name, res.get("error") or "对照失败"))
            continue
        met = res.get("metrics") or {}
        rows.append({"name": name, "score": met.get("score"),
                     "iou": met.get("iou"), "ssim": met.get("ssim"),
                     "aspect_delta": met.get("aspect_delta"),
                     "palette_delta": met.get("palette_delta")})
    if not rows:
        return {"ok": False, "text": "❌ 没有可对照的视图：%s"
                                     % ("、".join(skipped[:4]) or "缺少配对的图片"),
                "metrics": {}, "views": [], "error": "no_views"}

    scores = [float(r["score"]) for r in rows if r.get("score") is not None]
    ious = [float(r["iou"]) for r in rows if r.get("iou") is not None]
    ssims = [float(r["ssim"]) for r in rows if r.get("ssim") is not None]
    mean = round(sum(scores) / len(scores), 1) if scores else 0.0
    worst = min(rows, key=lambda r: float(r.get("score") or 0))
    best = max(rows, key=lambda r: float(r.get("score") or 0))
    failed = [r for r in rows if float(r.get("score") or 0) < float(min_score or 0)]
    metrics = {
        "views": len(rows),
        "views_failed": len(failed),
        "view_score": mean,
        "view_score_min": round(float(worst.get("score") or 0), 1),
        "view_score_max": round(float(best.get("score") or 0), 1),
        "view_iou": round(min(ious), 4) if ious else None,
        "view_ssim": round(min(ssims), 4) if ssims else None,
    }
    lines = ["多视图对照：%d 个视图 · 平均 %.1f/100 · 最差 %s %.1f"
             % (len(rows), mean, worst.get("name"), float(worst.get("score") or 0)),
             "阈值 %s：%d 个视图未达标" % (min_score, len(failed))]
    for r in sorted(rows, key=lambda x: float(x.get("score") or 0)):
        lines.append("  %s：%.1f 分 · IoU %.2f · SSIM %s · 比例差 %.0f%%%s"
                     % (r["name"], float(r.get("score") or 0),
                        float(r.get("iou") or 0),
                        ("%.2f" % r["ssim"]) if r.get("ssim") is not None else "—",
                        float(r.get("aspect_delta") or 0) * 100,
                        "  ← 最差" if r is worst else ""))
    if failed:
        lines.append("先改最差视图：%s（与参考的差异最大）" % worst.get("name"))
    elif len(rows) > 1:
        lines.append("所有视图都达到阈值；仍建议看一眼最差视图有没有语义问题。")
    if skipped:
        lines.append("未能对照：" + "；".join(skipped[:4]))
    lines.append("（逐视图按主体包围盒归一化后比较，适合发现比例 / 朝向 / 穿插；"
                 "不判断好不好看）")
    lines.append("__GEOMETRY_METRICS__ " + json.dumps(metrics, ensure_ascii=False))
    lines.append("__GEOMETRY_VIEWS__ " + json.dumps(rows, ensure_ascii=False))
    return {"ok": True, "text": "\n".join(lines), "metrics": metrics,
            "views": rows, "error": ""}
