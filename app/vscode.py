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


# ============================================================
# 当前颜色主题：让 IDE 模式「跟随 VS Code 正在用的主题」
# ============================================================
#
# 说明与边界：
#  · VS Code 是另一个进程，我们**无法订阅**它的主题切换事件；只能读它落盘的
#    settings.json（workbench.colorTheme）并按需轮询文件 mtime —— 所以是"近似实时"。
#  · 官方内置主题不在主题 JSON 里存工作台配色（那部分在 bundle 里），而我们已经为
#    Dark+ / Light+ / Dark Modern / Light Modern 做好了完整预设 → 命中时直接返回
#    预设 id，**不在 Python 里再抄一份色值**。
#  · 第三方主题才走"读主题 JSON → 按映射表取色"这条路；映射是**有损的**（我们只用到
#    约 24 个 token，而主题里有几百个），缺的项会回落。
#  · 语法高亮不跟随：任意主题的 tokenColors（TextMate scope）到 Monaco token 的转换
#    必然失真，所以编辑器语法色仍用与明暗匹配的内置 Dark+ / Light+。

_BUILTIN_THEME_PRESETS = {
    "Default Dark Modern": "vscode-modern-dark",
    "Default Light Modern": "vscode-modern-light",
    "Default Dark+": "vscode-dark",
    "Default Dark": "vscode-dark",
    "Dark+ (default dark)": "vscode-dark",
    "Default Light+": "vscode-light",
    "Default Light": "vscode-light",
    "Light+ (default light)": "vscode-light",
}

# 我们的 CSS token → VS Code 颜色 id（按优先级取第一个存在的）
_TOKEN_MAP = (
    ("--bg", ("editor.background",)),
    ("--bg-side", ("sideBar.background", "activityBar.background", "panel.background")),
    ("--bg-hover", ("list.hoverBackground", "toolbar.hoverBackground")),
    ("--bg-active", ("list.activeSelectionBackground", "list.inactiveSelectionBackground")),
    ("--bg-input", ("input.background", "dropdown.background")),
    ("--text", ("editor.foreground", "foreground")),
    ("--text-dim", ("descriptionForeground", "tab.inactiveForeground")),
    ("--border", ("panel.border", "editorGroup.border", "sideBar.border", "tab.border")),
    ("--border-strong", ("input.border", "widget.border", "dropdown.border")),
    ("--accent", ("button.background", "focusBorder", "activityBarBadge.background")),
    ("--accent-hover", ("button.hoverBackground",)),
    ("--on-accent", ("button.foreground",)),
    ("--bubble-user", ("button.background", "focusBorder")),
    ("--bubble-user-text", ("button.foreground",)),
    ("--code-bg", ("textCodeBlock.background",)),
    ("--code-head", ("editorWidget.background",)),
    ("--inline-bg", ("textPreformat.background",)),
    ("--quote-bar", ("textBlockQuote.border",)),
    ("--ide-activity-bg", ("activityBar.background",)),
    ("--ide-activity-fg", ("activityBar.foreground",)),
    ("--ide-activity-fg-dim", ("activityBar.inactiveForeground",)),
    ("--ide-tab-bg", ("tab.inactiveBackground", "editorGroupHeader.tabsBackground")),
    ("--ide-status-bg", ("statusBar.background",)),
    ("--ide-status-fg", ("statusBar.foreground", "foreground")),
    # 文件 / 列表项：VS Code 的资源管理器文件名走 sideBar.foreground，选中项与标签页各有自己的键，
    # 都不是"强调色"。（--ide-mod-fg 无 VS Code 对应物 —— 那是我们"AI 改过"的标记，不映射。）
    ("--ide-side-fg", ("sideBar.foreground", "foreground")),
    ("--ide-sel-fg", ("list.activeSelectionForeground", "foreground")),
    ("--ide-tab-fg", ("tab.activeForeground", "foreground")),
    ("--ide-tab-fg-dim", ("tab.inactiveForeground", "descriptionForeground")),
)

# Monaco 侧要的编辑器面层色（同样是 token → 颜色 id）
_EDITOR_MAP = (
    ("editor.background", ("editor.background",)),
    ("editor.foreground", ("editor.foreground", "foreground")),
    ("editorLineNumber.foreground", ("editorLineNumber.foreground",)),
    ("editorLineNumber.activeForeground", ("editorLineNumber.activeForeground",)),
    ("editorIndentGuide.background1", ("editorIndentGuide.background1", "editorIndentGuide.background")),
    ("editorIndentGuide.activeBackground1",
     ("editorIndentGuide.activeBackground1", "editorIndentGuide.activeBackground")),
    ("editor.selectionBackground", ("editor.selectionBackground",)),
    ("editorCursor.foreground", ("editorCursor.foreground",)),
    ("editorWidget.background", ("editorWidget.background",)),
    ("editorWidget.border", ("editorWidget.border", "widget.border")),
)


def _user_settings():
    """返回 (settings.json 路径, 解析后的 dict)；找不到/解析失败返回 ("", {})"""
    for d in _vscode_user_dirs():
        p = os.path.join(d, "settings.json")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return p, (json.loads(_strip_jsonc(f.read())) or {})
            except Exception:
                return p, {}
    return "", {}


def _theme_name(raw):
    """settings.json 里的 workbench.colorTheme；未设置时 VS Code 的出厂默认是 Dark Modern"""
    name = raw.get("workbench.colorTheme")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "Default Dark Modern"


def _vscode_ext_dirs():
    """内置扩展目录候选（从 code 命令位置反推安装目录），用于找内置主题文件"""
    cli = find_vscode_cli()
    out = []
    if cli:
        # <install>/bin/code.cmd → <install>
        base = os.path.dirname(os.path.dirname(os.path.abspath(cli)))
        if os.path.basename(base).lower() == "bin":
            base = os.path.dirname(base)
        app = os.path.join(base, "resources", "app", "extensions")
        if os.path.isdir(app):
            out.append(app)
    return out


_THEME_FILE_CACHE = {}      # 主题名 → (文件路径, ui_theme)；避免轮询时反复扫扩展目录


def _scan_theme_file(name):
    """在「内置扩展目录 + 用户扩展目录」里按 label / id 找主题 JSON。

    返回 (主题文件路径, ui_theme)；找不到返回 ("", "")。
    """
    def _match(contributes, ext_dir):
        for th in (contributes or {}).get("themes", []) or []:
            if not isinstance(th, dict):
                continue
            if th.get("label") == name or th.get("id") == name:
                rel = str(th.get("path") or "").lstrip("/")
                if not rel:
                    continue
                p = os.path.normpath(os.path.join(ext_dir, rel))
                if os.path.isfile(p):
                    return p, str(th.get("uiTheme") or "")

    roots = list(_vscode_ext_dirs())
    roots.append(os.path.join(os.path.expanduser("~"), ".vscode", "extensions"))
    for ext_root in roots:
        if not os.path.isdir(ext_root):
            continue
        try:
            names = sorted(os.listdir(ext_root))
        except Exception:
            continue
        for nm in names:
            pkg = os.path.join(ext_root, nm, "package.json")
            if not os.path.isfile(pkg):
                continue
            try:
                with open(pkg, "r", encoding="utf-8", errors="replace") as f:
                    meta = json.loads(_strip_jsonc(f.read())) or {}
            except Exception:
                continue
            hit = _match(meta.get("contributes"), os.path.join(ext_root, nm))
            if hit:
                return hit
    return "", ""


def _find_theme_file(name):
    """_scan_theme_file 的带缓存版本。

    前端会周期性轮询「当前 VS Code 主题」，不缓存的话每轮都要遍历扩展目录并逐个
    读 package.json，代价太大。缓存只按主题名做键 —— 主题被重新安装（路径变化）
    属于低频操作，重启进程即可刷新。
    """
    if name not in _THEME_FILE_CACHE:
        _THEME_FILE_CACHE[name] = _scan_theme_file(name)
    return _THEME_FILE_CACHE[name]


def _is_dark(hex_color, default=True):
    """按相对亮度判断明暗（用于挑 Monaco 基底与 color-scheme），不依赖主题声明"""
    s = (hex_color or "").strip().lstrip("#")
    if len(s) == 8:
        s = s[:6]
    if len(s) != 6:
        return default
    try:
        r, g, b = (int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:
        return default
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 0.5


def _stamp(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append("%s:%d:%d" % (os.path.basename(p), int(st.st_mtime), st.st_size))
        except Exception:
            out.append("-")
    return "|".join(out)


def current_theme_payload():
    """当前 VS Code 主题 → 预设 id 或 token 表（供 IDE 模式「跟随 VS Code」使用）"""
    spath, raw = _user_settings()
    name = _theme_name(raw)
    preset = _BUILTIN_THEME_PRESETS.get(name, "")
    if preset:
        return {
            "ok": True, "name": name, "preset": preset,
            "dark": not preset.endswith("light"),      # vscode-light / vscode-modern-light 为浅色
            "tokens": {}, "editor": {},
            "stamp": _stamp(spath), "source": spath,
        }

    tpath, ui = _find_theme_file(name)
    if not tpath:
        return {
            "ok": False, "name": name, "preset": "",
            "error": "找不到该主题的定义文件（第三方主题需已安装到本机）",
            "stamp": _stamp(spath),
        }
    try:
        with open(tpath, "r", encoding="utf-8", errors="replace") as f:
            theme = json.loads(_strip_jsonc(f.read())) or {}
    except Exception as e:
        return {"ok": False, "name": name, "preset": "",
                "error": "读取主题文件失败：%s" % e, "stamp": _stamp(spath, tpath)}

    colors = theme.get("colors") or {}
    tokens = {}
    for css, ids in _TOKEN_MAP:
        for cid in ids:
            v = colors.get(cid)
            if isinstance(v, str) and v.strip():
                tokens[css] = v.strip()
                break
    editor = {}
    for mid, ids in _EDITOR_MAP:
        for cid in ids:
            v = colors.get(cid)
            if isinstance(v, str) and v.strip():
                editor[mid] = v.strip()
                break
    bg = colors.get("editor.background")
    dark = _is_dark(bg) if isinstance(bg, str) else (ui != "vs")
    return {
        "ok": True, "name": name, "preset": "", "dark": dark,
        "tokens": tokens, "editor": editor,
        "stamp": _stamp(spath, tpath), "source": tpath,
        "theme_file": tpath,
    }
