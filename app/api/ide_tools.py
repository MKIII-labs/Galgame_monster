# -*- coding: utf-8 -*-
"""IDE 工具链接口：Python（ruff）/ C（gcc）的检查、构建与格式化。

统一约定：只调用本机已装好的工具；结果口径与 LSP 诊断一致（0 基行列 + Monaco severity）。
写盘类操作（格式化）走 `WRITE_LOCK` + `_snap` + `register_undo`，可在「AI 变更」里撤销。
"""
import os

from app import agent, toolchain
from app.registry import route


def _rel_in(sb, abs_path):
    return os.path.relpath(abs_path, sb).replace("\\", "/")


def _resolve(h, body):
    s, sb, err = h._sb_guard(body)
    if err:
        return None, None, None, err
    rel = str(body.get("path") or "").strip()
    if not rel:
        return None, None, None, h._json({"ok": False, "error": "缺少 path", "items": []}, 400)
    try:
        abs_path = agent.resolve_path(sb, rel)
    except agent.SandboxError as e:
        return None, None, None, h._json({"ok": False, "error": str(e), "items": []}, 400)
    if not os.path.isfile(abs_path):
        return None, None, None, h._json({"ok": False, "error": "文件不存在", "items": []}, 404)
    return s, sb, abs_path, None


@route("/api/ide/tools/status")
def _api_ide_tools_status(h):
    """前端据此决定显示哪些按钮 / 给什么提示。"""
    s, sb, err = h._sb_guard(h._body())
    if err:
        return err
    return h._json({"ok": True, "tools": toolchain.status()})


@route("/api/ide/py/lint")
def _api_ide_py_lint(h):
    body = h._body()
    s, sb, abs_path, err = _resolve(h, body)
    if err:
        return err
    rel = _rel_in(sb, abs_path)
    items, why = toolchain.ruff_check(rel, sb)
    if items is None:
        return h._json({"ok": False, "items": [], "error": why})
    return h._json({"ok": True, "items": items, "engine": "ruff"})


@route("/api/ide/c/check")
def _api_ide_c_check(h):
    body = h._body()
    s, sb, abs_path, err = _resolve(h, body)
    if err:
        return err
    rel = _rel_in(sb, abs_path)
    items, why = toolchain.gcc_check(rel, sb)
    if items is None:
        return h._json({"ok": False, "items": [], "error": why})
    return h._json({"ok": True, "items": items, "engine": "gcc"})


@route("/api/ide/c/build")
def _api_ide_c_build(h):
    body = h._body()
    s, sb, abs_path, err = _resolve(h, body)
    if err:
        return err
    rel = _rel_in(sb, abs_path)
    if not rel.lower().endswith(toolchain.C_EXTS):
        return h._json({"ok": False, "error": "只能构建 .c / .cpp 文件", "errors": []}, 400)
    with agent.BUILD_LOCK:               # 只产出 exe，不碰源码：不占 WRITE_LOCK
        res = toolchain.build_c(rel, sb)
    if res.get("ok"):
        agent.invalidate_tree(sb)
    return h._json(res)


@route("/api/ide/format")
def _api_ide_format(h):
    """就地格式化；结果登记 undo，可撤销。"""
    body = h._body()
    s, sb, abs_path, err = _resolve(h, body)
    if err:
        return err
    rel = _rel_in(sb, abs_path)
    with agent.WRITE_LOCK:
        try:
            snapshot = agent._snap(abs_path)
        except agent.SandboxError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        try:
            ok, note = toolchain.format_file(abs_path, sb)
        except OSError as e:
            return h._json({"ok": False, "error": "格式化失败：%s" % e}, 500)
        if ok is None:
            return h._json({"ok": False, "unsupported": True, "error": note})
        if not ok:
            return h._json({"ok": False, "error": note}, 500)
        token = agent.register_undo((s or {}).get("id") or "", [snapshot], "格式化：" + rel)
    agent.invalidate_tree(sb)
    return h._json({"ok": True, "token": token, "note": note})
