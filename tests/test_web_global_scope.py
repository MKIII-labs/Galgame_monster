# -*- coding: utf-8 -*-
"""前端全局作用域守门：同页加载的脚本，顶层声明不允许跨文件重名。

零构建链下几十个 <script> 共享一个全局作用域：两个文件声明同名顶层函数 / 变量时，
后者会静默覆盖前者，往往到运行时才暴露。这里按 index.html 的真实加载清单做静态扫描，
把这类冲突挡在提交前；新增脚本只要被 index.html 引用就会自动纳入检查。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"

DECL = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")
CLASS = re.compile(r"^class\s+([A-Za-z_$][\w$]*)")
VAR = re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)")


def _scripts():
    html = INDEX.read_text(encoding="utf-8")
    out = []
    for name in re.findall(r'src="/static/([^"]+\.js)"', html):
        if name.startswith("vendor/"):
            continue                     # 第三方库自带全局名，不参与检查
        out.append(ROOT / "web" / name)
    return out


def _declarations(path):
    """只认顶格声明：缩进的是块内作用域，不共享全局。"""
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = DECL.match(line) or CLASS.match(line) or VAR.match(line)
        if m:
            names.append(m.group(1))
    return names


def test_loaded_scripts_exist():
    scripts = _scripts()
    assert len(scripts) > 50, "index.html 的脚本清单解析异常（只找到 %d 个）" % len(scripts)
    missing = [str(p.relative_to(ROOT)) for p in scripts if not p.exists()]
    assert not missing, "index.html 引用了不存在的脚本：%s" % missing


def test_no_cross_file_top_level_duplicates():
    first = {}
    dup = {}
    for path in _scripts():
        if not path.exists():
            continue
        for name in _declarations(path):
            owner = first.setdefault(name, path)
            if owner != path:
                dup.setdefault(name, {str(owner.relative_to(ROOT))}).add(
                    str(path.relative_to(ROOT)))
    assert not dup, "顶层声明跨文件重名（后者会静默覆盖前者）：%s" % {
        k: sorted(v) for k, v in dup.items()}
