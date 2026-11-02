# -*- coding: utf-8 -*-
"""LaTeX 编译接口（IDE 用）：只调用本机已装好的引擎，不打包、不代下载。

- `/api/ide/tex/status`  有没有可用引擎（latexmk / xelatex / pdflatex）
- `/api/ide/tex/build`   编译主文件 → PDF + 结构化错误（文件:行号 + 消息）
- `/api/ide/tex/clean`   删辅助文件（.aux/.log/.synctex.gz…，PDF 不删）
- `/api/ide/tex/outline` 章节结构（正则解析，不编译）
"""
import os

from app import agent, latex
from app.config import load_config
from app.registry import route


def _rel_in(sb, abs_path):
    return os.path.relpath(abs_path, sb).replace("\\", "/")


@route("/api/ide/tex/status")
def _api_ide_tex_status(h):
    s, sb, err = h._sb_guard(h._body())
    if err:
        return err
    exe, kind = latex.find_engine(load_config() or {})
    return h._json({"ok": True, "engine": exe, "kind": kind, "available": bool(exe)})


@route("/api/ide/tex/build")
def _api_ide_tex_build(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    rel = str(body.get("path") or "").strip()
    if not rel:
        return h._json({"ok": False, "error": "缺少 path"}, 400)
    try:
        abs_tex = agent.resolve_path(sb, rel)
    except agent.SandboxError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    if not abs_tex.lower().endswith(".tex"):
        return h._json({"ok": False, "error": "只能编译 .tex 文件"}, 400)
    # 编译只写中间文件 / PDF，用 BUILD_LOCK 串行即可：
    # 拿 WRITE_LOCK 会让一次 15 秒的 latexmk 把 agent 的写操作全部堵住
    with agent.BUILD_LOCK:
        res = latex.build(sb, _rel_in(sb, abs_tex), load_config() or {})
    if res.get("ok"):
        agent.invalidate_tree(sb)
    return h._json(res)


@route("/api/ide/tex/clean")
def _api_ide_tex_clean(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    rel = str(body.get("path") or "").strip()
    if not rel:
        return h._json({"ok": False, "error": "缺少 path"}, 400)
    try:
        abs_tex = agent.resolve_path(sb, rel)
    except agent.SandboxError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    with agent.WRITE_LOCK:
        removed = latex.clean(sb, _rel_in(sb, abs_tex))
    agent.invalidate_tree(sb)
    return h._json({"ok": True, "removed": removed, "count": len(removed)})


@route("/api/ide/tex/outline")
def _api_ide_tex_outline(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    rel = str(body.get("path") or "").strip()
    if not rel:
        return h._json({"ok": False, "error": "缺少 path", "items": []}, 400)
    try:
        abs_tex = agent.resolve_path(sb, rel)
    except agent.SandboxError as e:
        return h._json({"ok": False, "error": str(e), "items": []}, 400)
    return h._json({"ok": True, "items": latex.outline(sb, _rel_in(sb, abs_tex))})
