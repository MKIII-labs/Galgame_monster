# -*- coding: utf-8 -*-
"""生成 app.ico：按标题栏同一条矢量几何（圆角方块 + 四角星）逐尺寸重绘。

为什么不直接缩放位图：旧 app.ico 的各帧是从一张低分辨率图缩出来的，16 / 32 帧在
任务栏、托盘和系统通知里明显发糊。这里每个尺寸都从矢量按 8 倍超采样画一次再降采样，
边缘是真正的抗锯齿，不做插值放大。

几何与配色以 web/index.html 里 .tb-logo 的 path 为准（32×32 坐标系），改图标时两边一起改。

用法：
    .venv\\Scripts\\python.exe scripts\\make_icon.py
"""
import io
import os
import struct

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "app.ico")
# 托盘气泡 / 系统通知会把小图标放大显示：单独导出一张 512px PNG，
# 由 desktop/main.js 传给 displayBalloon / Notification，避免用 16-24px 的托盘帧硬拉。
PNG_HD = os.path.join(ROOT, "app-512.png")

# 100% / 125% / 150% / 200% DPI 下任务栏与托盘会用到的尺寸都备一份
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
BLUE = (77, 107, 254, 255)       # #4d6bfe
WHITE = (255, 255, 255, 255)
SS = 8                            # 超采样倍数

# 与 web/index.html 的 .tb-logo 保持一致
STAR = [(16, 5), (18, 13.88), (27, 16), (18.12, 17.88),
        (16, 27), (13.88, 18), (5, 16), (13.75, 14)]
RECT = (0.75, 0.75, 31.25, 31.25)
RADIUS = 5.6


def render(size):
    n = size * SS
    k = n / 32.0
    # 小尺寸（16 / 20 / 24）把星形放大一点：1px 宽的尖角在任务栏和托盘里
    # 抗锯齿后会被冲淡成一团灰白，放大 22% 才能看清「四角星」这个轮廓。
    boost = 1.22 if size <= 24 else 1.0
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([RECT[0] * k, RECT[1] * k, RECT[2] * k, RECT[3] * k],
                        radius=RADIUS * k, fill=BLUE)
    d.polygon([((16 + (x - 16) * boost) * k, (16 + (y - 16) * boost) * k)
               for x, y in STAR], fill=WHITE)
    return im.resize((size, size), Image.LANCZOS)


def png_bytes(im):
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def write_ico(path, images):
    """Vista 起支持 PNG 帧；旧文件也是全 PNG 帧，保持一致。"""
    frames = [(im, png_bytes(im)) for im in images]
    head = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = b""
    blobs = b""
    for im, data in frames:
        w, h = im.size
        entries += struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    with open(path, "wb") as f:
        f.write(head + entries + blobs)


def main():
    images = [render(s) for s in SIZES]
    write_ico(OUT, images)
    render(512).save(PNG_HD, "PNG", optimize=True)
    print("wrote %s  sizes=%s  bytes=%d" % (OUT, SIZES, os.path.getsize(OUT)))
    print("wrote %s  bytes=%d" % (PNG_HD, os.path.getsize(PNG_HD)))


if __name__ == "__main__":
    main()
