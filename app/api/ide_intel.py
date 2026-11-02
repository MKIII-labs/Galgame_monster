# -*- coding: utf-8 -*-
"""IDE 语言服务接口（编辑器前台化）。

背景：`app/lsp.py` 的能力以前只给 agent 用（模型调 lsp_diagnostics 才知道哪有错），
用户在编辑器里看不到。这里把这套能力暴露成结构化 HTTP 接口：

- 诊断 → Monaco 波浪线 + 问题面板
- 符号 → 大纲
- 定义 / 引用 / 实现 → 右键跳转
- 重命名 → 先出方案（apply=false），确认后再写盘（apply=true），写盘走 undo 栈

另外提供「内联编辑」：选中一段代码 + 一句指令 → 返回替换后的代码，
由前端直接写进编辑器缓冲区（不落盘，用户可 Ctrl+Z / Ctrl+S）。

只做 Python（pyright）；其它语言由前端直接跳过，不在这里报错。
"""
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse

from app import agent, lsp, winproc
from app.config import get_api_conf
from app.llm import _quick_chat
from app.registry import route


def _rel_of(root, abs_path):
    try:
        return os.path.relpath(abs_path, root).replace("\\", "/")
    except ValueError:
        return abs_path


@route("/api/ide/lsp/status")
def _api_ide_lsp_status(h):
    """前端先问一句，避免在没装 pyright 的机器上反复弹错。"""
    s, sb, err = h._sb_guard(h._body())
    if err:
        return err
    return h._json({"ok": True, "available": lsp.available(), "root": sb})


@route("/api/ide/lsp/diagnostics")
def _api_ide_lsp_diagnostics(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    rel = str(body.get("path") or "").strip()
    if not rel:
        return h._json({"ok": False, "error": "缺少 path", "items": []}, 400)
    try:
        abs_path = agent.resolve_path(sb, rel)
    except agent.SandboxError as e:
        return h._json({"ok": False, "error": str(e), "items": []}, 400)
    items, why = lsp.diagnostics_items(sb, abs_path)
    return h._json({"ok": not why, "items": items, "error": why})


@route("/api/ide/lsp/symbols")
def _api_ide_lsp_symbols(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    rel = str(body.get("path") or "").strip()
    if not rel:
        return h._json({"ok": False, "error": "缺少 path", "items": []}, 400)
    try:
        abs_path = agent.resolve_path(sb, rel)
    except agent.SandboxError as e:
        return h._json({"ok": False, "error": str(e), "items": []}, 400)
    items, why = lsp.symbol_items(sb, abs_path)
    return h._json({"ok": not why, "items": items, "error": why})


@route("/api/ide/lsp/goto")
def _api_ide_lsp_goto(h):
    """kind = definition / references / implementation；行列都从 1 开始。"""
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    rel = str(body.get("path") or "").strip()
    if not rel:
        return h._json({"ok": False, "error": "缺少 path", "items": []}, 400)
    try:
        abs_path = agent.resolve_path(sb, rel)
    except agent.SandboxError as e:
        return h._json({"ok": False, "error": str(e), "items": []}, 400)
    items, why = lsp.goto_items(sb, str(body.get("kind") or "definition"), abs_path,
                                body.get("line") or 1, body.get("col") or 0,
                                str(body.get("symbol") or ""))
    return h._json({"ok": not why, "items": items, "error": why})


@route("/api/ide/lsp/hover")
def _api_ide_lsp_hover(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    rel = str(body.get("path") or "").strip()
    if not rel:
        return h._json({"ok": False, "error": "缺少 path", "text": ""}, 400)
    try:
        abs_path = agent.resolve_path(sb, rel)
    except agent.SandboxError as e:
        return h._json({"ok": False, "error": str(e), "text": ""}, 400)
    text, why = lsp.hover_text(sb, abs_path, body.get("line") or 1,
                               body.get("col") or 0, str(body.get("symbol") or ""))
    return h._json({"ok": bool(text), "text": text, "error": why})


@route("/api/ide/lsp/rename")
def _api_ide_lsp_rename(h):
    """两段式：apply=false 只出方案（不写盘），apply=true 才真正落盘。"""
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    rel = str(body.get("path") or "").strip()
    new_name = str(body.get("new_name") or "").strip()
    if not rel:
        return h._json({"ok": False, "error": "缺少 path"}, 400)
    if not new_name:
        return h._json({"ok": False, "error": "缺少 new_name"}, 400)
    try:
        abs_path = agent.resolve_path(sb, rel)
    except agent.SandboxError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    files, note = lsp.rename_plan(sb, abs_path, body.get("line") or 1,
                                  str(body.get("symbol") or ""), new_name)
    if not files:
        return h._json({"ok": False, "error": note, "files": [], "count": 0})
    plan = [{"path": _rel_of(sb, f["path"]), "lines": _changed_lines(f)}
            for f in files]
    if not body.get("apply"):
        return h._json({"ok": True, "files": plan, "count": len(files),
                        "note": note})
    undo_ops, written = [], []
    try:
        with agent.WRITE_LOCK:          # 和 agent / 子代理的写操作互斥
            for f in files:
                undo_ops.append(agent._snap(f["path"]))
                with open(f["path"], "w", encoding="utf-8", newline="") as fh:
                    fh.write(f["new"])
                written.append(_rel_of(sb, f["path"]))
    except OSError as e:
        # 写失败就把已经写过的先回滚，避免只改了一半
        for op in reversed(undo_ops):
            if op.get("kind") != "restore":
                continue
            try:
                if op.get("existed"):
                    with open(op["path"], "wb") as f:
                        f.write(op.get("data") or b"")
                elif os.path.exists(op["path"]):
                    os.remove(op["path"])
            except OSError:
                pass
        return h._json({"ok": False, "error": "写入失败：%s" % e}, 400)
    agent.invalidate_tree(sb)
    token = agent.register_undo((s or {}).get("id") or "",
                                undo_ops, "重命名为 " + new_name)
    return h._json({"ok": True, "files": written, "count": len(written),
                    "token": token, "note": note})


def _changed_lines(f):
    """方案里每个文件改了几行：只做展示，不参与写盘。"""
    old = (f.get("old") or "").splitlines()
    new = (f.get("new") or "").splitlines()
    diff = sum(1 for a, b in zip(old, new) if a != b)
    return diff + abs(len(old) - len(new))


# ---------------- Markdown / HTML → PDF（本机 Edge 无头打印） ----------------

@route("/api/ide/export_pdf")
def _api_ide_export_pdf(h):
    """把前端渲染好的 HTML 打成 PDF，落在源文件同目录（同名 .pdf）。

    Markdown → HTML 的渲染在前端（要复用 KaTeX / 既有排版），
    这里只负责「写临时 HTML → headless Edge --print-to-pdf → 收进沙箱」。
    """
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    html = str(body.get("html") or "")
    if not html.strip():
        return h._json({"ok": False, "error": "没有可导出的内容"}, 400)
    name = str(body.get("name") or "preview.md").strip().replace("\\", "/")
    try:
        src_abs = agent.resolve_path(sb, name)
    except agent.SandboxError as e:
        return h._json({"ok": False, "error": str(e)}, 400)
    from app import webprobe
    exe = webprobe.edge_path()
    if not exe:
        return h._json({"ok": False, "error": "找不到 Edge / Chrome，无法生成 PDF"}, 400)
    stem = os.path.splitext(os.path.basename(src_abs))[0] or "preview"
    out_abs = os.path.join(os.path.dirname(src_abs), stem + ".pdf")
    tmpdir = tempfile.mkdtemp(prefix="gm_pdf_")
    tmp_html = os.path.join(tmpdir, "page.html")
    try:
        with open(tmp_html, "w", encoding="utf-8") as f:
            f.write(html)
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
               "--print-to-pdf=" + out_abs,
               "file:///" + tmp_html.replace("\\", "/")]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=90,
                                  creationflags=winproc.no_window())
        except subprocess.TimeoutExpired:
            return h._json({"ok": False, "error": "PDF 生成超时（90 秒）"}, 500)
        if not os.path.isfile(out_abs):
            msg = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
            return h._json({"ok": False, "error": "PDF 生成失败：%s"
                            % (msg or "浏览器没有输出文件")}, 500)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    agent.invalidate_tree(sb)
    return h._json({"ok": True, "path": _rel_of(sb, out_abs),
                    "size": os.path.getsize(out_abs)})


# ---------------- 内置网页预览：沙箱目录临时挂到本机端口 ----------------

def _preview_rel(sandbox, rel):
    """把前端给的预览路径收敛成沙箱内相对路径；越界 / 敏感文件 / 不存在都抛 SandboxError。"""
    rel = str(rel or "").strip().replace("\\", "/").lstrip("/")
    abs_path = agent.resolve_path(sandbox, rel, for_read=True)
    if agent.is_secret_path(abs_path):
        raise agent.SandboxError("这是密钥 / 凭据类文件，预览不提供访问")
    if not os.path.exists(abs_path):
        raise agent.SandboxError("找不到文件：%s" % rel)
    out = os.path.relpath(abs_path, os.path.abspath(sandbox))
    return "" if out in (".", "") else out.replace("\\", "/")


@route("/api/ide/preview/start")
def _api_ide_preview_start(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    rel = str(body.get("path") or "").strip()
    if rel:
        try:
            rel = _preview_rel(sb, rel)
        except agent.SandboxError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
    from app import preview
    try:
        base = preview.start(sb)
    except OSError as e:
        return h._json({"ok": False, "error": "预览服务启动失败：%s" % e}, 500)
    url = (base + urllib.parse.quote(rel, safe="/")) if rel else base
    return h._json({"ok": True, "base": base, "url": url})


@route("/api/ide/preview/stop")
def _api_ide_preview_stop(h):
    from app import preview
    preview.stop()
    return h._json({"ok": True})


# ---------------- 内联编辑：选中 → 一句指令 → 替换后的代码 ----------------

_INLINE_SYSTEM = "\n".join([
    "你是代码编辑器里的「内联改写」助手。用户会给你：文件路径、语言、",
    "选中的代码、选区前后的上下文、以及一句修改要求。",
    "硬性规则：",
    "1. 只输出**替换选中内容后的代码**，不要解释、不要客套、不要 Markdown 代码围栏。",
    "2. 不要重复选区之外的内容（上下文只用于理解，不要回写）。",
    "3. 保持原有的缩进风格、命名风格与语言习惯；能少改就少改。",
    "4. 如果要求无法完成或选区本身有语法错误，输出一行：// ERROR: 原因（不要输出半成品代码）。",
])


def _strip_fence(text):
    """模型偶尔仍会套 ```：剥掉最外层围栏，避免把围栏写进代码。"""
    t = str(text or "").strip()
    m = re.match(r"^```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?)\n?```$", t)
    return m.group(1) if m else t


@route("/api/ide/inline_edit")
def _api_ide_inline_edit(h):
    body = h._body()
    s, sb, err = h._sb_guard(body)
    if err:
        return err
    instruction = str(body.get("instruction") or "").strip()
    sel = str(body.get("selection") or "")
    if not instruction:
        return h._json({"ok": False, "error": "缺少修改要求"}, 400)
    if not sel.strip():
        return h._json({"ok": False, "error": "先选中要改的代码"}, 400)
    rel = str(body.get("path") or "")
    lang = str(body.get("language") or "text")
    before = str(body.get("before") or "")[-4000:]
    after = str(body.get("after") or "")[:2000]
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return h._json({"ok": False, "error": "未配置聊天接口密钥"}, 400)
    user = ("文件：%s\n语言：%s\n\n【选区前的上下文】\n%s\n\n【选中的代码（要被替换）】\n%s\n\n"
            "【选区后的上下文】\n%s\n\n修改要求：%s"
            % (rel or "（未命名）", lang, before, sel, after, instruction))
    try:
        raw = _quick_chat(api_key, api_url, api_model,
                          [{"role": "system", "content": _INLINE_SYSTEM},
                           {"role": "user", "content": user}],
                          max_tokens=1500, source="ide-inline", timeout=(8, 60),
                          tries=1, use_cache=False, temperature=0.2)
    except Exception as e:                      # noqa: BLE001
        return h._json({"ok": False, "error": "调用模型失败：%s" % str(e)[:200]}, 502)
    text = _strip_fence(raw)
    if text.startswith("// ERROR:"):
        return h._json({"ok": False, "error": text[len("// ERROR:"):].strip()[:400]})
    if not text.strip():
        return h._json({"ok": False, "error": "模型没有返回内容"})
    return h._json({"ok": True, "text": text[:20000]})
