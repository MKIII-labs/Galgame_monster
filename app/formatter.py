# -*- coding: utf-8 -*-
"""写后自动格式化（本地工具，不消耗 token）。

只做一件事：把模型刚写好的文件交给格式化器，拿回结果覆盖原文件。

设计取舍：
- **只调用本机已安装的工具**，不用 `npx` 临时下载 —— 首次下载会让一次写文件卡十几秒，
  而格式化是锦上添花，不值得阻塞主流程。
- **失败一律保留原文件**：格式化器版本不兼容 / 超时 / 输出为空，都当没发生。
- **stdin → stdout 模式**：不原地写盘，结果交给调用方走原有写路径，
  这样撤销栈、变更审阅、写后语法校验全部复用。
- 格式化后由调用方**重新做一次语法校验**，避免格式化器把坏文件改得更坏。
"""
import os
import shutil
import subprocess

MAX_BYTES = 512 * 1024
TIMEOUT = 20

_PY = (".py", ".pyw")
_JS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue")


def _which(name):
    return shutil.which(name) or shutil.which(name + ".cmd")


def available():
    """本机有没有任何可用的格式化器（仅用于设置页展示）。"""
    return bool(_which("ruff") or _which("prettier"))


def _run(cmd, text, cwd):
    try:
        p = subprocess.run(
            cmd, input=text.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=TIMEOUT, cwd=cwd or None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        return p.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def format_file(path, mode="auto"):
    """格式化一个文件。

    返回 (是否改动, 说明)。说明只在出错时非空，用于回灌模型；
    正常格式化不产生额外 token。
    """
    mode = str(mode or "off").strip().lower()
    if mode in ("", "off", "false", "0", "none"):
        return False, ""
    try:
        if not path or not os.path.isfile(path):
            return False, ""
        if os.path.getsize(path) > MAX_BYTES:
            return False, ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False, ""
    if not text.strip():
        return False, ""

    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    cwd = os.path.dirname(path)

    if ext in _PY:
        exe = _which("ruff")
        if not exe:
            return False, ""
        out = _run([exe, "format", "-", "--stdin-filename", name], text, cwd)
    elif ext in _JS:
        exe = _which("prettier")
        if not exe:
            return False, ""
        out = _run([exe, "--stdin-filepath", name], text, cwd)
    else:
        return False, ""

    if out is None or not out.strip() or out == text:
        return False, ""
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(out)
    except OSError as e:
        return False, "格式化结果写回失败：%s" % e
    return True, ""
