# -*- coding: utf-8 -*-
"""跨语言项目探测与轻量语法自检。

目标不是「支持所有语言的全部工具链」，而是**不把非 Python 项目挡在门外**：
- 探测项目标记，告诉模型该用哪套构建 / 测试命令（只提示，不自动执行）；
- 写后自检只做「本机有解释器就能快速验证」的语法检查，缺工具就静默跳过；
- 符号/索引等能力不支持的语言，由调用方降级到 grep / 全文检索，而不是报错。
"""
import json
import os
import shutil
import subprocess

from app import winproc

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 12

CODE_EXTS = {
    ".py", ".pyw", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".go", ".rs", ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".cxx",
    ".hpp", ".cs", ".rb", ".php", ".lua", ".swift", ".dart", ".scala", ".sh",
    ".ps1", ".sql", ".html", ".css", ".scss", ".less", ".json", ".yml", ".yaml",
    ".toml", ".xml",
}

CHECK_EXTS = {".py", ".pyw", ".json", ".js", ".mjs", ".cjs", ".go"}

# 符号搜索只扫源码，不扫配置 / 文档，避免 find_symbol 噪声与无谓 IO
SOURCE_EXTS = CODE_EXTS - {".json", ".yml", ".yaml", ".toml", ".xml", ".md", ".txt",
                           ".csv", ".env", ".ini", ".cfg", ".conf"}

_LANGS = {
    ".py": "Python", ".pyw": "Python", ".js": "JavaScript", ".mjs": "JavaScript",
    ".cjs": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".vue": "Vue", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".c": "C", ".h": "C/C++", ".cc": "C++", ".cpp": "C++", ".hpp": "C++",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".lua": "Lua", ".swift": "Swift",
    ".dart": "Dart", ".scala": "Scala",
}


def language_of(path):
    return _LANGS.get(os.path.splitext(str(path or ""))[1].lower(), "")


def _exists(root, name):
    return os.path.isfile(os.path.join(root, name))


def _pkg_scripts(root):
    p = os.path.join(root, "package.json")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data.get("scripts") if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _node_pm(root):
    if _exists(root, "pnpm-lock.yaml"):
        return "pnpm"
    if _exists(root, "yarn.lock"):
        return "yarn"
    if _exists(root, "bun.lockb") or _exists(root, "bun.lock"):
        return "bun"
    return "npm"


def detect(root):
    """扫项目根目录的标记文件，返回语言 / 包管理器 / 构建 / 测试命令。"""
    out = {"languages": [], "package_manager": "", "build": [], "test": [],
           "markers": []}
    root = str(root or "")
    if not root or not os.path.isdir(root):
        return out
    add_lang = out["languages"].append
    add_build = out["build"].append
    add_test = out["test"].append

    if _exists(root, "pyproject.toml") or _exists(root, "setup.py") \
            or _exists(root, "requirements.txt"):
        add_lang("Python")
        out["markers"].append("python")
        pm = "uv" if _exists(root, "uv.lock") else (
            "poetry" if _exists(root, "poetry.lock") else "pip")
        out["package_manager"] = pm
        add_test("python -m pytest -q")
        if pm == "uv":
            add_build("uv build")
        elif pm == "poetry":
            add_build("poetry build")
        else:
            add_build("python -m build")

    if _exists(root, "package.json"):
        add_lang("Node")
        out["markers"].append("node")
        pm = _node_pm(root)
        out["package_manager"] = pm
        scripts = _pkg_scripts(root)
        if "test" in scripts:
            add_test("%s test" % pm)
        if "build" in scripts:
            add_build("%s run build" % pm)
        if _exists(root, "tsconfig.json"):
            add_lang("TypeScript")

    if _exists(root, "Cargo.toml"):
        add_lang("Rust")
        out["markers"].append("rust")
        out["package_manager"] = out["package_manager"] or "cargo"
        add_test("cargo test")
        add_build("cargo build")

    if _exists(root, "go.mod"):
        add_lang("Go")
        out["markers"].append("go")
        out["package_manager"] = out["package_manager"] or "go"
        add_test("go test ./...")
        add_build("go build ./...")

    if _exists(root, "pom.xml"):
        add_lang("Java")
        out["markers"].append("maven")
        out["package_manager"] = out["package_manager"] or "maven"
        add_test("mvn test")
        add_build("mvn package")
    elif _exists(root, "build.gradle") or _exists(root, "build.gradle.kts"):
        add_lang("Java")
        out["markers"].append("gradle")
        out["package_manager"] = out["package_manager"] or "gradle"
        add_test("gradle test")
        add_build("gradle build")

    try:
        for fn in os.listdir(root):
            low = fn.lower()
            if low.endswith(".csproj") or low.endswith(".sln"):
                add_lang("C#")
                out["markers"].append("dotnet")
                out["package_manager"] = out["package_manager"] or "dotnet"
                add_test("dotnet test")
                add_build("dotnet build")
                break
    except OSError:
        pass

    if _exists(root, "CMakeLists.txt"):
        add_lang("C/C++")
        out["markers"].append("cmake")
        out["package_manager"] = out["package_manager"] or "cmake"
        add_build("cmake --build build")
    if _exists(root, "Makefile") or _exists(root, "makefile"):
        out["markers"].append("make")
        add_build("make")
    return out


def profile_line(root):
    """给提示词用的一行项目画像；没有识别到就返回空串。"""
    info = detect(root)
    langs = list(dict.fromkeys(info.get("languages") or []))
    if not langs:
        return ""
    bits = ["语言：" + " / ".join(langs[:3])]
    if info.get("package_manager"):
        bits.append("包管理：" + info["package_manager"])
    if info.get("build"):
        bits.append("构建：" + info["build"][0])
    if info.get("test"):
        bits.append("测试：" + info["test"][0])
    return "【项目画像】" + " · ".join(bits)


def _run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                           creationflags=winproc.no_window())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return p


def syntax_check(path):
    """快速语法检查；返回错误文本，没问题或无法检查时返回 ""。"""
    try:
        if not path or not os.path.isfile(path):
            return ""
        if os.path.getsize(path) <= 0 or os.path.getsize(path) > MAX_BYTES:
            return ""
        ext = os.path.splitext(path)[1].lower()
        name = os.path.basename(path)
        if ext in (".py", ".pyw"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
            try:
                compile(src, name, "exec")
            except SyntaxError as e:
                return "第 %s 行：%s" % (e.lineno or "?", e.msg)
            return ""
        if ext == ".json":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            try:
                json.loads(raw)
            except ValueError as e:
                return str(e)
            return ""
        if ext in (".js", ".mjs", ".cjs"):
            p = _run([shutil.which("node") or "node", "--check", path])
            if p and p.returncode != 0:
                msg = (p.stderr or p.stdout or "").strip()
                return msg[-600:] if msg else "node --check 失败"
            return ""
        if ext == ".go":
            gofmt = shutil.which("gofmt")
            if not gofmt:
                return ""
            p = _run([gofmt, "-e", path])
            if p and p.returncode != 0:
                msg = (p.stderr or "").strip()
                return msg[-600:] if msg else "gofmt 解析失败"
            return ""
        return ""
    except Exception:                  # noqa: BLE001  自检失败不能影响写入
        return ""


def lsp_kind(path):
    """当前有内置 LSP 客户端的语言；其它语言由调用方降级到检索。"""
    ext = os.path.splitext(str(path or ""))[1].lower()
    return "python" if ext in (".py", ".pyi", ".pyw") else ""


def lsp_fallback(path):
    lang = language_of(path) or "该语言"
    return ("%s 暂无内置 LSP，已跳过语义查询。可改用 code(action=find / context / check) "
            "或 read(action=search) 做正则检索。" % lang)
