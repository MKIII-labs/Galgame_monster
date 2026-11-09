# -*- coding: utf-8 -*-
"""模拟股市前端一致性核验（可复跑）。

股票页要后端建局后才 mount，headless 打开 index.html 触达不到，
所以这里用源码级检查兜住最容易出错的几类问题：

1. querySelector('#stock-*') 引用的 id，是否都在模板里有对应 id="..."
2. 模板里是否有重复 id
3. JS 里用到的 stock-* 类名，是否在 CSS 里有定义（含 hall.css / base.css）
4. 关键接线是否存在：限价单传参、风险面板加载、报告导出

用法： python scripts/check_stock_ui.py
退出码 0 = 全部通过。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "web", "hall-stock.js")
CSS_FILES = [
    os.path.join(ROOT, "web", "css", "hall-stock.css"),
    os.path.join(ROOT, "web", "css", "hall.css"),
    os.path.join(ROOT, "web", "css", "base.css"),
]


def read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def main():
    js = read(JS)
    css = "\n".join(read(p) for p in CSS_FILES if os.path.exists(p))
    fails = []

    # ---- 1. id="..." 定义 ----
    defined = set(re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", js))
    # 重复 id（模板里同一 id 出现多次会让 querySelector 拿到错的元素）
    all_ids = re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", js)
    dup = sorted({i for i in all_ids if all_ids.count(i) > 1})
    # 只关心 stock- 前缀（页面其它区域由别处负责）
    dup = [d for d in dup if d.startswith("stock-")]

    # ---- 2. 被引用的 id ----
    refs = set(re.findall(r"""querySelector\(\s*['"]#([A-Za-z0-9_-]+)['"]""", js))
    refs |= set(re.findall(r"""getElementById\(\s*['"]([A-Za-z0-9_-]+)['"]""", js))
    missing = sorted(r for r in refs if r.startswith("stock-") and r not in defined)

    # ---- 3. 类名覆盖 ----
    used = set()
    for m in re.findall(r"""class=["']([^"']+)["']""", js):
        used |= {c for c in m.split() if c.startswith("stock-")}
    for m in re.findall(r"""className\s*=\s*['"]([^'"]+)['"]""", js):
        used |= {c for c in m.split() if c.startswith("stock-")}
    for m in re.findall(r"""classList\.add\(\s*['"]([^'"]+)['"]""", js):
        used |= {c for c in m.split() if c.startswith("stock-")}
    css_classes = set(re.findall(r"""\.([A-Za-z0-9_-]+)""", css))
    unstyled = sorted(c for c in used if c not in css_classes)

    # ---- 4. 关键接线 ----
    wiring = [
        ("限价单传参", "limit_price"),
        ("风险面板加载", "stockLoadRisk"),
        ("报告导出", "stockExportReport"),
        ("快捷仓位", "stockFillQuick"),
        # 十字光标与标签页切换都是内联实现，没有独立函数名，
        # 所以按实际标识符探测（crossG = 光标图元组，data-tab + stock-tabpanel = 标签页）
        ("十字光标", "crossG"),
        ("侧栏标签切换", "stock-tabpanel"),
    ]
    missing_wiring = [name for name, needle in wiring if needle not in js]

    print("JS 行数：%d，模板 id 定义 %d 个，被引用 id %d 个" % (
        len(js.splitlines()), len(defined), len(refs)))
    print("CSS 类：%d 个（含 hall-stock/hall/base）" % len(css_classes))

    if missing:
        fails.append("引用了不存在的 id：%s" % ", ".join(missing))
    if dup:
        fails.append("模板里重复的 id：%s" % ", ".join(dup))
    if unstyled:
        fails.append("有类名在 CSS 中找不到定义：%s" % ", ".join(unstyled))
    if missing_wiring:
        fails.append("关键接线缺失：%s" % ", ".join(missing_wiring))

    # 计划里点名要有的新样式
    for cls in ("stock-tabs", "stock-tabpanel", "stock-quick", "stock-risk-row"):
        if cls not in css_classes:
            fails.append("缺少新样式：.%s" % cls)

    if fails:
        print("\n不通过：")
        for f in fails:
            print("  - " + f)
        return 1
    print("\n全部通过：id 引用齐全、无重复 id、类名都有样式、关键接线就位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
