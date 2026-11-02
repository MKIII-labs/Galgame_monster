# -*- coding: utf-8 -*-
"""LaTeX 编译支持：只用用户机器上已装好的引擎，不打包、不代下载。

和 VS Code 的 LaTeX Workshop 是同一个模型：探测 `latexmk` / `xelatex` / `pdflatex`
→ 调用 → 解析日志 → 把错误回填成「文件:行号 + 消息」。

安全边界：
- 永不传 `-shell-escape`（TeX 的 shell escape 等于任意命令执行）
- cwd 限定在 .tex 所在目录（调用方保证在沙箱内）
- 硬超时，超时连子进程一起杀（latexmk 会派生引擎进程）
"""
import os
import re
import shutil
import subprocess
import time

from app import winproc

TIMEOUT = 180                 # 单次编译上限（秒）；中文 + 交叉引用通常 10–30 秒
MAX_LOG = 20000               # 回传日志上限
MAX_ERRORS = 40
# 编译产物 / 辅助文件（清理由用户显式触发；PDF 永远不删）
AUX_EXTS = (".aux", ".log", ".out", ".toc", ".lof", ".lot", ".fls", ".fdb_latexmk",
            ".synctex.gz", ".synctex", ".bbl", ".blg", ".nav", ".snm", ".vrb", ".xdv",
            ".ilg", ".ind", ".idx", ".glo", ".gls", ".glg", ".run.xml", ".bcf",
            ".pdf.bak")     # 编译失败但进程被强杀时可能残留的备份


def _kind_of(path):
    low = os.path.basename(path or "").lower()
    if "latexmk" in low:
        return "latexmk"
    if "xelatex" in low:
        return "xelatex"
    if "lualatex" in low:
        return "lualatex"
    if "pdflatex" in low:
        return "pdflatex"
    return ""


def find_engine(cfg=None):
    """返回 (可执行文件路径, 类型)。找不到返回 ("", "")。

    优先用 config.json 的 `latex_engine`（绝对路径或命令名），
    否则按 latexmk → xelatex → pdflatex 的顺序找 PATH。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    custom = str(cfg.get("latex_engine") or "").strip()
    if custom:
        if os.path.isfile(custom):
            return custom, _kind_of(custom)
        found = shutil.which(custom)
        if found:
            return found, _kind_of(found)
    for name in ("latexmk", "xelatex", "pdflatex"):
        found = shutil.which(name)
        if found:
            return found, name
    return "", ""


def _kill_tree(pid):
    """超时后连子进程一起杀掉（latexmk 会再派生一个引擎进程）。"""
    from app import agent
    agent._kill_tree(pid)


def parse_errors(log):
    """从日志里抽错误。返回 [{file?, line?, message, context}]。

    两种来源：
    - `-file-line-error`：`./chap1.tex:12: Undefined control sequence`
    - 传统格式：`! Undefined control sequence.` + 后面几行的 `l.12 ...`
    """
    out, seen = [], set()
    lines = (log or "").splitlines()
    for ln in lines:
        m = re.match(r"^(?:\./)?([^\s:]+\.(?:tex|sty|cls)):(\d+):\s*(.+)$", ln)
        if not m:
            continue
        key = (m.group(1), m.group(2), m.group(3)[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append({"file": m.group(1), "line": int(m.group(2)),
                    "message": m.group(3).strip()[:300], "context": ""})
        if len(out) >= MAX_ERRORS:
            return out
    for i, ln in enumerate(lines):
        if not ln.startswith("!"):
            continue
        msg = ln[1:].strip()
        if not msg:
            continue
        line_no, ctx = None, ""
        for j in range(i + 1, min(i + 9, len(lines))):
            m = re.match(r"l\.(\d+)\s?(.*)", lines[j])
            if m:
                line_no = int(m.group(1))
                ctx = m.group(2).strip()
                break
        key = (line_no, msg[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append({"line": line_no, "message": msg[:300], "context": ctx[:200]})
        if len(out) >= MAX_ERRORS:
            break
    return out


def parse_warnings(log):
    out = []
    for ln in (log or "").splitlines():
        if ln.startswith("LaTeX Warning:") or re.match(r"^Package \S+ Warning:", ln):
            out.append(ln.strip()[:300])
        if len(out) >= 10:
            break
    return out


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def build(root, rel, cfg=None):
    """编译一个 .tex 主文件。返回结构化结果（不抛异常）。

    成功：{ok: True, pdf, engine, ms, warnings, log}
    失败：{ok: False, error, errors[], log, ms}
    """
    abs_tex = os.path.abspath(os.path.join(root, rel))
    if not os.path.isfile(abs_tex):
        return {"ok": False, "error": "文件不存在：%s" % rel, "errors": []}
    src = _read(abs_tex)
    if "\\documentclass" not in src:
        return {"ok": False, "errors": [],
                "error": "这个文件里没有 \\documentclass，请在主文件上编译（子文件不能单独编译）"}
    exe, kind = find_engine(cfg)
    if not exe:
        return {"ok": False, "errors": [],
                "error": "没找到 LaTeX 引擎（latexmk / xelatex / pdflatex）。"
                         "请先安装 TeX Live 或 MiKTeX，或在设置里填 latex_engine"}
    workdir = os.path.dirname(abs_tex)
    stem = os.path.splitext(os.path.basename(abs_tex))[0]
    pdf_abs = os.path.join(workdir, stem + ".pdf")
    # 不能直接删旧 PDF：编译失败时那是用户唯一能看的一版。
    # 先改名备份，成功删备份、失败还原（还原时会丢掉这次生成的半成品）。
    bak_abs = pdf_abs + ".bak"
    had_old = False
    try:
        if os.path.isfile(bak_abs):
            os.remove(bak_abs)
        if os.path.isfile(pdf_abs):
            os.replace(pdf_abs, bak_abs)
            had_old = True
    except OSError:
        had_old = False

    base = [exe]
    if kind == "latexmk":
        base += ["-xelatex", "-interaction=nonstopmode", "-file-line-error", "-synctex=1"]
    else:
        base += ["-interaction=nonstopmode", "-file-line-error", "-synctex=1"]
    base.append(os.path.basename(abs_tex))
    # 没有 latexmk 时跑两遍：第二遍解析交叉引用 / 目录
    rounds = [base] if kind == "latexmk" else [base, base]

    t0 = time.time()
    raw_out = ""
    failed = False
    def _fail(res):
        """失败时把上一版 PDF 还原回来（这次生成的半成品丢掉），并标记 restored。"""
        if had_old:
            try:
                if os.path.isfile(pdf_abs):
                    os.remove(pdf_abs)
                os.replace(bak_abs, pdf_abs)
                res["restored"] = True
            except OSError:
                pass
        return res

    for cmd in rounds:
        try:
            proc = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    creationflags=winproc.no_window())
        except OSError as e:
            return _fail({"ok": False, "error": "启动 %s 失败：%s" % (kind, e), "errors": []})
        try:
            out, _ = proc.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            return _fail({"ok": False, "ms": int((time.time() - t0) * 1000),
                          "error": "编译超时（%d 秒）" % TIMEOUT,
                          "errors": parse_errors(raw_out)})
        raw_out += (out or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            failed = True
            break

    ms = int((time.time() - t0) * 1000)
    log_path = os.path.join(workdir, stem + ".log")
    log_text = _read(log_path) or raw_out
    errors = parse_errors(log_text)
    ok = os.path.isfile(pdf_abs) and not failed
    pdf_rel = os.path.relpath(pdf_abs, root).replace("\\", "/")
    res = {"ok": ok, "pdf": pdf_rel if ok else "", "engine": kind, "ms": ms,
           "errors": errors, "warnings": parse_warnings(log_text),
           "log": log_text[-MAX_LOG:]}
    if ok:
        try:
            if os.path.isfile(bak_abs):
                os.remove(bak_abs)
        except OSError:
            pass
    else:
        _fail(res)
    if not ok and not res.get("error"):
        if errors:
            res["error"] = errors[0]["message"]
        else:
            res["error"] = "编译失败（退出码非 0，日志里没有解析到具体错误）"
    return res


def clean(root, rel):
    """删除辅助文件（保留 PDF 与源文件）。返回删掉的文件名列表。"""
    abs_tex = os.path.abspath(os.path.join(root, rel))
    workdir = os.path.dirname(abs_tex)
    stem = os.path.splitext(os.path.basename(abs_tex))[0]
    removed = []
    for ext in AUX_EXTS:
        p = os.path.join(workdir, stem + ext)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed.append(stem + ext)
            except OSError:
                pass
    return removed


_OUTLINE_RE = re.compile(
    r"\\(chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")
_OUTLINE_DEPTH = {"chapter": 0, "section": 0, "subsection": 1, "subsubsection": 2,
                  "paragraph": 3}


def outline(root, rel):
    """用正则抽章节结构（不编译、不依赖语言服务）。返回 items 列表。"""
    abs_tex = os.path.abspath(os.path.join(root, rel))
    if not os.path.isfile(abs_tex):
        return []
    text = _read(abs_tex)
    items = []
    for i, line in enumerate(text.splitlines()):
        for m in _OUTLINE_RE.finditer(line):
            name = re.sub(r"\s+", " ", m.group(2)).strip()
            if not name:
                continue
            items.append({"name": name, "detail": "\\" + m.group(1),
                          "line": i + 1,
                          "depth": _OUTLINE_DEPTH.get(m.group(1), 0)})
            if len(items) >= 200:
                return items
    return items
