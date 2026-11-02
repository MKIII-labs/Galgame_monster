# -*- coding: utf-8 -*-
"""外部工具链：只调用本机已装好的工具（ruff / gcc / clang-format），不打包、不代下载。

和 LaTeX 支持同一个模型：探测 → 调用 → 解析输出 → 回填问题面板。
没有 clangd 时，C 的诊断用 `gcc -fsyntax-only` 兜底（能报错，但没有补全/跳转）。
"""
import json
import os
import re
import shutil
import subprocess

from app import winproc

TIMEOUT_CHECK = 60
TIMEOUT_BUILD = 120
MAX_ITEMS = 80
C_EXTS = (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx")
PY_EXTS = (".py", ".pyi")

_GCC_DIAG = re.compile(
    r"^(?P<file>[^:\n]+?):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<kind>fatal error|error|warning|note):\s*(?P<msg>.+)$")


def find(name):
    return shutil.which(name) or ""


def status():
    """前端用它决定显示哪些按钮 / 给什么提示。"""
    return {
        "python": find("python"),
        "ruff": find("ruff"),
        "gcc": find("gcc") or find("clang"),
        "cxx": find("g++") or find("clang++"),
        "gdb": find("gdb") or find("lldb"),
        "clangd": find("clangd"),
        "clang_format": find("clang-format"),
    }


def _run(cmd, cwd, timeout):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                           creationflags=winproc.no_window())
    except FileNotFoundError:
        return 127, "", "找不到命令：%s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "执行超时（%d 秒）" % timeout
    except OSError as e:
        return 1, "", str(e)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    # gcc 在中文 Windows 上可能按 GBK 输出
    if not err and p.stderr:
        err = p.stderr.decode("gbk", "replace")
    return p.returncode, out, err


# ---------------------------------------------------------------- Python

def ruff_check(path, cwd):
    """ruff check → (items, error)。items 口径与 LSP 诊断一致（0 基行列 + Monaco severity）。"""
    exe = find("ruff")
    if not exe:
        return None, "未安装 ruff"
    code, out, err = _run([exe, "check", "--output-format=json", "--no-cache", path],
                          cwd, TIMEOUT_CHECK)
    if code == 127:
        return None, err
    try:
        data = json.loads(out or "[]")
    except ValueError:
        return None, "ruff 输出无法解析：%s" % (err or out)[:200]
    items = []
    for d in data:
        if not isinstance(d, dict):
            continue
        loc = d.get("location") or {}
        code_s = str(d.get("code") or "")
        sev = 8 if code_s[:1] in ("E", "F") else 4
        items.append({
            "line": max(0, int(loc.get("row") or 1) - 1),
            "col": max(0, int(loc.get("column") or 1) - 1),
            "endLine": max(0, int(loc.get("row") or 1) - 1),
            "endCol": int(loc.get("column") or 1),
            "severity": sev,
            "message": str(d.get("message") or "")[:300],
            "code": code_s,
        })
        if len(items) >= MAX_ITEMS:
            break
    return items, ""


# ---------------------------------------------------------------- C / C++

def gcc_check(path, cwd):
    """`gcc -fsyntax-only` 当轻量诊断（没有 clangd 时的兜底）。"""
    exe = find("gcc") or find("clang")
    if not exe:
        return None, "未安装 gcc / clang"
    code, out, err = _run([exe, "-fsyntax-only", "-Wall", "-Wextra",
                           "-fdiagnostics-color=never", path], cwd, TIMEOUT_CHECK)
    text = (err or "") + ("\n" + out if out else "")
    items = []
    for ln in text.splitlines():
        m = _GCC_DIAG.match(ln.strip())
        if not m:
            continue
        kind = m.group("kind")
        if kind == "note":
            continue
        items.append({
            "line": max(0, int(m.group("line")) - 1),
            "col": max(0, int(m.group("col")) - 1),
            "endLine": max(0, int(m.group("line")) - 1),
            "endCol": int(m.group("col")),
            "severity": 8 if kind in ("error", "fatal error") else 4,
            "message": m.group("msg").strip()[:300],
            "code": kind,
            "file": m.group("file").replace("\\", "/"),
        })
        if len(items) >= MAX_ITEMS:
            break
    if code == 127:
        return None, err
    return items, ""


def build_c(path, cwd):
    """编译成可执行文件（放在同目录）。返回 {ok, exe, errors, log}。"""
    exe = find("gcc") or find("clang")
    if not exe:
        return {"ok": False, "error": "未安装 gcc / clang", "errors": []}
    stem = os.path.splitext(os.path.basename(path))[0]
    out_name = stem + (".exe" if os.name == "nt" else "")
    code, out, err = _run([exe, "-g", "-O0", "-Wall", "-Wextra",
                           "-fdiagnostics-color=never", "-o", out_name, path],
                          cwd, TIMEOUT_BUILD)
    items, _ = gcc_check(path, cwd) if code != 0 else ([], "")
    ok = code == 0 and os.path.isfile(os.path.join(cwd, out_name))
    res = {"ok": ok, "exe": out_name if ok else "", "errors": items or [],
           "log": ((out or "") + (err or ""))[-8000:]}
    if not ok:
        if res["errors"]:
            res["error"] = res["errors"][0].get("message") or "编译失败"
        else:
            res["error"] = (err or out or "编译失败").strip().splitlines()[-1][:300]
    return res


# ---------------------------------------------------------------- 格式化

def format_file(path, cwd):
    """就地格式化。返回 (是否成功/None 表示不支持, 说明)。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in PY_EXTS:
        exe = find("ruff")
        if not exe:
            return None, "未安装 ruff（Python 格式化需要它）"
        code, out, err = _run([exe, "format", path], cwd, TIMEOUT_CHECK)
        if code != 0:
            return False, (err or out or "格式化失败").strip()[:400]
        return True, "已用 ruff 格式化"
    if ext in C_EXTS:
        exe = find("clang-format")
        if not exe:
            return None, "未安装 clang-format（gcc 不带格式化器）"
        code, out, err = _run([exe, "-i", path], cwd, TIMEOUT_CHECK)
        if code != 0:
            return False, (err or out or "格式化失败").strip()[:400]
        return True, "已用 clang-format 格式化"
    return None, "这个文件类型不支持格式化"
