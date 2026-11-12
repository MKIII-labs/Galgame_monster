# -*- coding: utf-8 -*-
import json
import os
import re
import shutil

# ---------- VS Code 联动（阶段 3：外部打开 + 只读导入配置） ----------

def find_vscode_cli():
    """定位 Windows 上的 code 命令（PATH 优先，其次常见安装目录）"""
    for name in ("code", "code.cmd"):
        p = shutil.which(name)
        if p:
            return p
    la = os.environ.get("LOCALAPPDATA") or ""
    pf = os.environ.get("ProgramFiles") or ""
    pf86 = os.environ.get("ProgramFiles(x86)") or ""
    for base in (os.path.join(la, "Programs", "Microsoft VS Code"),
                 os.path.join(pf, "Microsoft VS Code"),
                 os.path.join(pf86, "Microsoft VS Code")):
        p = os.path.join(base, "bin", "code.cmd")
        if os.path.isfile(p):
            return p
    return ""


def _vscode_user_dirs():
    """VS Code 用户配置目录候选（含 Insiders）"""
    ap = os.environ.get("APPDATA") or ""
    return [os.path.join(ap, n, "User") for n in ("Code", "Code - Insiders")]


def _strip_jsonc(text):
    """去掉 JSONC 的 // 与 /* */ 注释和尾逗号，好让 json.loads 能吃"""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def vscode_import_payload():
    """只读读取本机 VS Code 的设置 / 快捷键路径 / 扩展清单（不修改任何文件）"""
    settings_path = ""
    user_dir = ""
    for d in _vscode_user_dirs():
        sp = os.path.join(d, "settings.json")
        if os.path.isfile(sp):
            settings_path = sp
            user_dir = d
            break
    if not user_dir:
        for d in _vscode_user_dirs():
            if os.path.isdir(d):
                user_dir = d
                settings_path = os.path.join(d, "settings.json")
                break
    keybindings_path = ""
    if user_dir:
        kb = os.path.join(user_dir, "keybindings.json")
        if os.path.isfile(kb):
            keybindings_path = kb

    raw = {}
    if settings_path and os.path.isfile(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8", errors="replace") as f:
                raw = json.loads(_strip_jsonc(f.read())) or {}
        except Exception:
            raw = {}

    def pick(dotted):
        # VS Code 的 settings.json 用的是「扁平点号键」（如 "editor.fontSize"），
        # 优先按字面键取；取不到再按嵌套对象兜底。
        if dotted in raw:
            return raw[dotted]
        cur = raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    settings = {}
    fs = pick("editor.fontSize")
    ts = pick("editor.tabSize")
    if isinstance(fs, (int, float)):
        settings["fontSize"] = int(fs)
    if isinstance(ts, (int, float)):
        settings["tabSize"] = int(ts)
    ff = pick("editor.fontFamily")
    if isinstance(ff, str) and ff.strip():
        settings["fontFamily"] = ff.strip()
    ww = pick("editor.wordWrap")
    if isinstance(ww, str) and ww.strip():
        settings["wordWrap"] = ww.strip()
    rw = pick("editor.renderWhitespace")
    if isinstance(rw, str) and rw.strip():
        settings["renderWhitespace"] = rw.strip()
    mz = pick("editor.mouseWheelZoom")
    if isinstance(mz, bool):
        settings["mouseWheelZoom"] = mz
    settings["_count"] = len(raw)

    ext_dir = os.path.join(os.path.expanduser("~"), ".vscode", "extensions")
    exts = []
    if os.path.isdir(ext_dir):
        try:
            for name in sorted(os.listdir(ext_dir)):
                if name.startswith(".") or name == "extensions.json":
                    continue
                if not os.path.isdir(os.path.join(ext_dir, name)):
                    continue
                parts = name.rsplit("-", 1)
                exts.append(parts[0] if len(parts) == 2 else name)
        except Exception:
            exts = []

    return {
        "ok": True,
        "settings": settings,
        "extensions": exts,
        "paths": {
            "settings": settings_path or "",
            "keybindings": keybindings_path or "",
            "extensions": ext_dir if os.path.isdir(ext_dir) else "",
        },
    }
