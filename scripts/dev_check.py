# -*- coding: utf-8 -*-
"""一键自检（不需要 pytest）：
    python scripts/dev_check.py

做三件事：
  1. 全量语法编译（app/*.py + 入口）
  2. 全量导入 app.* 模块（抓 import 期错误）
  3. 起一次服务端（不监听）做冒烟 + 跑几条关键不变量断言

用于「改完代码先自查」，比手工起服务快。"""
import os
import py_compile
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# 自检会调用 create_server；不关掉自动恢复的话，会把真实未完成目标拉起来跑。
os.environ.setdefault("GALGAME_NO_AUTORESUME", "1")

FAIL = []


def ok(msg):
    print("  [OK]   " + msg)


def bad(msg):
    print("  [FAIL] " + msg)
    FAIL.append(msg)


def step(title):
    print("\n== " + title + " ==")


def check_compile():
    step("1. 语法编译")
    targets = [os.path.join(ROOT, "main.py"), os.path.join(ROOT, "server.py")]
    app_dir = os.path.join(ROOT, "app")
    targets += [os.path.join(app_dir, f) for f in sorted(os.listdir(app_dir)) if f.endswith(".py")]
    for p in targets:
        try:
            py_compile.compile(p, doraise=True)
        except py_compile.PyCompileError as e:
            bad("%s 编译失败：%s" % (os.path.relpath(p, ROOT), e))
            return
    ok("全部 %d 个文件编译通过" % len(targets))


def check_imports():
    step("2. 模块导入")
    app_dir = os.path.join(ROOT, "app")
    mods = [f[:-3] for f in sorted(os.listdir(app_dir))
            if f.endswith(".py") and f != "__init__.py"]
    for m in mods:
        try:
            __import__("app." + m)
        except Exception as e:
            bad("import app.%s 失败：%s: %s" % (m, type(e).__name__, e))
            return
    ok("全部 %d 个模块导入通过" % len(mods))


def check_invariants():
    step("3. 关键不变量")
    from app import gal, knowledge
    from app.saves import RESTORE_FIELDS

    # 3.1 提示词里不得写死具体世界观名词（本会话踩过的耦合）
    neutral = {"name": "测试角色", "profile": "普通设定。"}
    node = {"id": "a1", "type": "limited_turn", "maxRounds": 4, "prompt": "x"}

    def _template_only(t, marks=("### 可用背景", "### 可用 BGM", "### 当前舞台现状")):
        """剥离按用户数据拼进来的段落（背景/BGM 清单等），只查代码模板本身"""
        cut = len(t)
        for m in marks:
            i = t.find(m)
            if i >= 0:
                cut = min(cut, i)
        return t[:cut]

    texts = {
        "build_prompt": _template_only(gal.build_prompt(neutral)),
        "build_script_prompt": gal.build_script_prompt(neutral, world="占位"),
        "build_next_act_prompt": gal.build_next_act_prompt(neutral, world="占位"),
        "build_node_prompt": gal.build_node_prompt(node, 1),
        "build_recap_prompt": gal.build_recap_prompt("a", "b"),
    }
    banned = ("选择光幕", "光幕", "非凡者", "序列九", "解析系统")
    hits = [(n, b) for n, t in texts.items() for b in banned if b in t]
    if hits:
        bad("提示词写死了世界观名词：%s" % hits)
    else:
        ok("提示词未写死任何具体世界观名词")

    # 3.2 演出契约必须还在
    p = gal.build_prompt(neutral)
    need = ("## GAL 演出模式", "dialog", "options", "cast", "map", "stats")
    miss = [k for k in need if k not in p]
    (bad if miss else ok)("演出契约字段缺失：%s" % miss if miss else "演出契约字段齐全")

    # 3.3 分幕大纲五要素
    sp = gal.build_script_prompt(neutral)
    miss2 = [k for k in ("情境", "目标", "阻碍", "转折", "代价与钩子") if k not in sp]
    (bad if miss2 else ok)("分幕大纲要素缺失：%s" % miss2 if miss2 else "分幕大纲五要素齐全")

    # 3.4 规则常驻 / 检索排除 两态：不能「排除后无替代」
    kb = {"id": "kb_t", "name": "T", "nodes": [
        {"id": "n1", "title": "光幕规则", "content": "带倒计时。", "category": "规则"}],
        "edges": [], "graph": {}}
    fake = {"kb_ids": ["kb_t"], "memory": {"nodes": []}}
    old_load, old_prof = knowledge.load_kb, knowledge.get_profile_kb
    knowledge.load_kb = lambda kid: kb if kid == "kb_t" else None
    knowledge.get_profile_kb = lambda: "kb_profile"
    try:
        t, n = knowledge.pinned_block(fake)
        if n != 1 or "光幕规则" not in t:
            bad("pinned_block 未按类别收集规则节点")
        else:
            ok("规则常驻：%d 条" % n)
        blk_off, _ = knowledge.retrieve_memory(fake, "光幕", exclude_pinned=False)
        blk_on, _ = knowledge.retrieve_memory(fake, "光幕", exclude_pinned=True)
        if "光幕规则" not in blk_off:
            bad("非 GAL 检索丢了规则节点（回归！）")
        elif "光幕规则" in blk_on:
            bad("GAL 检索未排除规则节点（会与常驻重复）")
        else:
            ok("检索两态正确（非 GAL 保留 / GAL 排除）")
    finally:
        knowledge.load_kb, knowledge.get_profile_kb = old_load, old_prof

    # 3.5 存档可恢复字段必须含背景与前情提要
    for k in ("gal_bg", "gal_recap", "gal_auto_story"):
        if k not in RESTORE_FIELDS:
            bad("RESTORE_FIELDS 缺少 %s（回退/分支会丢状态）" % k)
    ok("RESTORE_FIELDS 含 gal_bg / gal_recap / gal_auto_story")


def check_server_smoke():
    step("4. 服务端冒烟")
    try:
        from app.httpd import create_server
        srv, port = create_server(port=0)
        srv.server_close()
        ok("create_server 成功（port %d）" % port)
    except Exception as e:
        bad("create_server 失败：%s: %s" % (type(e).__name__, e))


def check_frontend():
    """前端 JS 语法闸门：一个文件一段（拆分后靠它防「边界切错」）。"""
    step("5. 前端语法（node --check）")
    node = shutil.which("node")
    if not node:
        print("  [跳过] 未找到 node（可选依赖）")
        return
    web = os.path.join(ROOT, "web")
    files = []
    for root, dirs, names in os.walk(web):
        dirs[:] = [d for d in dirs if d != "vendor"]
        files += [os.path.join(root, f) for f in names if f.endswith(".js")]
    bads = []
    for p in sorted(files):
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
        if r.returncode != 0:
            msg = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1]
            bads.append("%s：%s" % (os.path.relpath(p, ROOT), msg))
    if bads:
        for b in bads:
            bad("前端语法错误 " + b)
    else:
        ok("全部 %d 个前端 JS 语法通过" % len(files))


def check_startup():
    """启动耗时：create_server 要播种素材 / 建路由表，变慢通常意味着启动期做了重活。"""
    step("8. 启动耗时")
    try:
        from app.httpd import create_server
    except Exception as e:
        bad("导入 httpd 失败：%s: %s" % (type(e).__name__, e))
        return
    t0 = time.perf_counter()
    srv, port = create_server(port=0)
    ms = (time.perf_counter() - t0) * 1000
    srv.server_close()
    ok("create_server 用时 %.0f ms（port %d）" % (ms, port))


def check_hotkeys_test():
    """快捷键单元测试（node scripts/hotkeys_test.js，零依赖）。

    覆盖：按键串解析与展示名、录制校验（裸键闸门）、命中优先级（用户自定义优先）、
    同/跨作用域重键检测、e.code → Monaco 键位换算、内置枚举快照一致性、
    以及注册表自校验（作用域合法 / editor 规则带 mono / 默认键可解析 / 同作用域不重键）。
    """
    step("6. 快捷键单元测试（node scripts/hotkeys_test.js）")
    node = shutil.which("node")
    if not node:
        print("  [跳过] 未找到 node（可选依赖）")
        return
    script = os.path.join(ROOT, "scripts", "hotkeys_test.js")
    if not os.path.isfile(script):
        bad("找不到 scripts/hotkeys_test.js")
        return
    r = subprocess.run([node, script], capture_output=True, text=True, cwd=ROOT)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    for line in out.splitlines():
        print("  " + line)
    if r.returncode == 0:
        ok("快捷键单元测试通过")
    else:
        bad("快捷键单元测试未通过")


def check_markdown_test():
    """Markdown 渲染回归：不完整围栏、公式、表格、裸工具 JSON、异常兜底。"""
    step("7. Markdown 渲染回归（node scripts/markdown_test.js）")
    node = shutil.which("node")
    if not node:
        print("  [跳过] 未找到 node（可选依赖）")
        return
    script = os.path.join(ROOT, "scripts", "markdown_test.js")
    if not os.path.isfile(script):
        bad("找不到 scripts/markdown_test.js")
        return
    r = subprocess.run([node, script], capture_output=True, text=True, cwd=ROOT)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    for line in out.splitlines():
        print("  " + line)
    if r.returncode == 0:
        ok("Markdown 渲染回归通过")
    else:
        bad("Markdown 渲染回归未通过")


def main():
    print("=" * 56)
    print("  项目自检（dev_check）")
    print("=" * 56)
    check_compile()
    check_imports()
    check_invariants()
    check_server_smoke()
    check_frontend()
    check_hotkeys_test()
    check_markdown_test()
    check_startup()
    print("\n" + "=" * 56)
    if FAIL:
        print("  ✗ 未通过 %d 项：" % len(FAIL))
        for f in FAIL:
            print("    - " + f)
        return 1
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
