# -*- coding: utf-8 -*-
"""Computer Use · 截图落盘

把截图保存到会话沙箱的 `.gm/screenshots/`，返回相对路径；前端通过
`/api/sandbox/raw` 带鉴权读取，再显示在工具结果卡里。
"""
import base64
import glob
import os
import time
import uuid

KEEP = 100          # 每个沙箱只保留最近 100 张，避免长会话把目录撑大


def save_png(b64, sandbox, prefix="shot"):
    """保存 base64 PNG；成功返回沙箱相对路径，失败返回空串。"""
    if not b64 or not sandbox:
        return ""
    try:
        root = os.path.abspath(sandbox)
        d = os.path.join(root, ".gm", "screenshots")
        os.makedirs(d, exist_ok=True)
        name = "%s_%s_%s.png" % (prefix, time.strftime("%Y%m%d_%H%M%S"),
                                 uuid.uuid4().hex[:6])
        with open(os.path.join(d, name), "wb") as f:
            f.write(base64.b64decode(b64))
        files = sorted(glob.glob(os.path.join(d, "*.png")),
                       key=os.path.getmtime, reverse=True)
        for old in files[KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass
        return ".gm/screenshots/" + name
    except Exception:
        return ""
