# -*- coding: utf-8 -*-
"""智能体预览入口：打开沙箱 HTML，或把本机 dev server 代理到 IDE 预览面板。"""
import os
import urllib.parse


def _t_open_preview(root, args, caps=None):
    from app import preview
    from .sandbox import _rel, resolve_path
    a = args or {}
    url = str(a.get("url") or "").strip()
    if url:
        try:
            base = preview.start_dev(url)
        except ValueError as e:
            return "❌ %s" % e, None
        except OSError as e:
            return "❌ 预览代理启动失败：%s" % e, None
        return ("预览已启动（本机 dev server 代理，控制台 / 元素选择可用）：\n%s\n"
                "GM_PREVIEW_URL:%s" % (base, base)), None
    rel = str(a.get("path") or "").strip()
    if not rel:
        return "❌ 需要 path（沙箱内 HTML）或 url（本机 dev server）", None
    p = resolve_path(root, rel, for_read=True)
    if not os.path.isfile(p):
        return "文件不存在：" + _rel(root, p), None
    if not str(p).lower().endswith((".html", ".htm")):
        return "❌ path 只支持 .html / .htm；dev server 请用 url", None
    base = preview.start(root)
    target = base + urllib.parse.quote(_rel(root, p) or "", safe="/")
    return ("预览已启动：\n%s\nGM_PREVIEW_URL:%s" % (target, target)), None
