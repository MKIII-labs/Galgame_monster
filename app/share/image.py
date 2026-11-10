# -*- coding: utf-8 -*-
"""分享长图：把自包含 HTML 用本机 Edge/Chrome 渲染成整页 PNG。

两步：
1. `--dump-dom` 跑一次带测量脚本的副本，拿到 `documentElement.scrollHeight`；
2. 用这个高度再来一次 `--screenshot`，最后用 Pillow 裁掉上下多余留白。
没有浏览器时返回 0（调用方降级提示改用 HTML）。
"""
import os
import re
import tempfile

from app import webprobe

_MEASURE = (
    "<script>window.addEventListener('load',function(){"
    "var d=document.createElement('meta');d.name='sh-h';"
    "d.content=String(Math.ceil(document.documentElement.scrollHeight));"
    "document.head.appendChild(d);});</script>"
)
_H_RE = re.compile(r'name="sh-h"\s+content="(\d+)"|content="(\d+)"\s+name="sh-h"')

MAX_H = 16000      # 单张图高度上限（再长就截断，避免浏览器画布爆掉）
MIN_H = 600


def _measure(edge, html_path, width, work):
    try:
        with open(html_path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return 0
    if "</body>" in src:
        src = src.replace("</body>", _MEASURE + "</body>")
    else:
        src += _MEASURE
    dst = os.path.join(work, "measure.html")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(src)
    url = "file:///" + dst.replace("\\", "/").lstrip("/")
    args = ["--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
            "--user-data-dir=" + os.path.join(work, "mprof"),
            "--window-size=%d,%d" % (width, 1200),
            "--virtual-time-budget=5000", "--dump-dom", url]
    p = webprobe._run(edge, args, 90)
    dom = p.stdout.decode("utf-8", "replace")
    m = _H_RE.search(dom)
    if not m:
        return 0
    return int(m.group(1) or m.group(2) or 0)


def _trim(path):
    """裁掉与背景同色的上下留白；返回是否成功。"""
    try:
        from PIL import Image
    except ImportError:
        return True
    try:
        im = Image.open(path).convert("RGB")
    except Exception:                     # noqa: BLE001
        return False
    w, h = im.size
    px = im.load()
    bg = px[2, 2] if w > 3 and h > 3 else (255, 255, 255)

    def row_has_content(y):
        for x in range(0, w, 7):
            if px[x, y] != bg:
                return True
        return False

    top, bottom = 0, h - 1
    for y in range(0, h):
        if row_has_content(y):
            top = y
            break
    for y in range(h - 1, -1, -1):
        if row_has_content(y):
            bottom = y
            break
    pad = 16
    box = (0, max(0, top - pad), w, min(h, bottom + 1 + pad))
    if box[3] - box[1] < 60:
        return False
    im.crop(box).save(path)
    return True


def html_to_png(html_path, out_path, width=760):
    """渲染整页长图；成功返回字节数，失败返回 0。"""
    edge = webprobe.edge_path()
    if not edge:
        return 0
    work = tempfile.mkdtemp(prefix="gm_share_png_")
    try:
        h = _measure(edge, html_path, width, work) or 2400
        h = max(MIN_H, min(MAX_H, h + 8))
        url = "file:///" + os.path.abspath(html_path).replace("\\", "/").lstrip("/")
        b64 = webprobe._shot(edge, url, os.path.join(work, "sprof"),
                             width, h, out_path, 120)
        if not b64 or not os.path.isfile(out_path):
            return 0
        _trim(out_path)
        return os.path.getsize(out_path)
    finally:
        try:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
        except Exception:                 # noqa: BLE001
            pass
