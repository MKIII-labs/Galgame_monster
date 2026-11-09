# -*- coding: utf-8 -*-
"""模拟股市重构的回归检查：门面符号完整性 + stockkit 无循环导入 + 撮合行为抽检。

用法：``python scripts/check_stock_refactor.py``，退出码 0 表示全部通过。

为什么用 AST 而不是正则找 ``stock.<name>``：正则会命中 "app/stock.py" 这类
路径字符串，把 ``py`` 当成属性名，产生假失败（重构时就踩过这个坑）。
"""
import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

FAIL = 0


def bad(msg):
    global FAIL
    FAIL += 1
    print("FAIL", msg)


# 1) AST 抓 `stock.<name>` 属性访问（只看真正的属性引用）
names = set()
for base in ("app", "scripts", "tests"):
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, base)):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".venv")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "stock"):
                    names.add(node.attr)

from app import stock  # noqa: E402

missing = sorted(n for n in names if not hasattr(stock, n))
print("引用的 stock.* 符号数：%d，缺失：%d" % (len(names), len(missing)))
if missing:
    bad("门面缺少符号：%s" % ", ".join(missing))

# 2) stockkit 各子模块单独冷启动导入（能单独导入说明没有向上依赖 stock）
mods = ["errors", "rules", "quotes", "matching", "corporate", "risk", "report"]
for mod in mods:
    code = "import app.stockkit.%s as m; print(m.__name__)" % mod
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        bad("app.stockkit.%s 无法独立导入：%s" % (mod, (r.stderr or "").strip()[-300:]))
    else:
        print("单独导入 ok:", r.stdout.strip())

# 3) 反向依赖检查：stockkit 不许 import app.stock（否则成环）
for mod in mods:
    path = os.path.join(ROOT, "app", "stockkit", mod + ".py")
    text = open(path, encoding="utf-8").read()
    if re.search(r"^\s*(from|import)\s+app\.stock\b", text, re.M):
        bad("app/stockkit/%s.py 反向依赖 app.stock（会成环）" % mod)

# 4) 关键对外入口可调用
for fn in ("game_state", "list_games", "risk_metrics", "export_report",
           "corporate_actions", "adjusted_bars", "place_order", "cancel_order"):
    f = getattr(stock, fn, None)
    if f is None:
        bad("缺少入口 %s" % fn)
    elif not callable(f):
        bad("%s 不可调用" % fn)
    else:
        print("入口 ok:", fn)

# 5) 撮合内核的纯函数行为抽检（不落库）
from app.stockkit import matching  # noqa: E402

bar = {"date": "2026-08-03", "open": 10.0, "high": 10.5, "low": 9.8,
       "close": 10.2, "volume": 3000}

# 市价买入 1000 股，当日量 3000 → 参与率 10% = 300 股，按 100 股整手 → 300
fill = matching.plan_fill("buy", 1000, bar, prev_close=9.9, limit=0.1, symbol="sh600000")
print("plan_fill 市价买 ->", fill)
if fill.qty != 300:
    bad("A 股市价买部分成交应为 300 股，实际 %s" % fill.qty)
if fill.qty % 100 != 0:
    bad("A 股撮合没按手数取整：%s" % fill)

# 币市按 1e-6 取整，不能被当成整手
cbar = {"date": "2026-08-03", "open": 100.0, "high": 101.0, "low": 99.0,
        "close": 100.5, "volume": 5.5}
cfill = matching.plan_fill("buy", 1.0, cbar, prev_close=100.0, limit=0.0, symbol="BTCUSDT")
print("plan_fill 币市买 ->", cfill)
if cfill.qty <= 0:
    bad("币市小额买被错误判定为不成交：%s" % cfill)
# 用 1e-9 容差比较，避免浮点取模误报
if abs(cfill.qty * 1e6 - round(cfill.qty * 1e6)) > 1e-6:
    bad("币市成交没按 1e-6 取整：%s" % cfill.qty)

# 限价买 a) 开盘价已在限价内 → 按更优的开盘价成交
lfill = matching.plan_fill("buy", 100, bar, prev_close=9.9, limit=0.1,
                           limit_price=10.05, symbol="sh600000")
print("plan_fill 限价买(开盘在限价内) ->", lfill)
if lfill.qty <= 0 or abs(lfill.price - 10.0) > 1e-6:
    bad("限价买开盘在限价内应按开盘价成交：%s" % lfill)

# 限价买 b) 开盘价高于限价、盘中最低价触及 → 按限价成交
ifill = matching.plan_fill("buy", 100, bar, prev_close=10.5, limit=0.2,
                           limit_price=9.9, symbol="sh600000")
print("plan_fill 限价买(盘中触及) ->", ifill)
if ifill.qty <= 0 or abs(ifill.price - 9.9) > 1e-6:
    bad("限价买盘中触及应按限价成交：%s" % ifill)

# 开盘涨停：市价买判废
zbar = {"date": "2026-08-03", "open": 11.0, "high": 11.0, "low": 11.0,
        "close": 11.0, "volume": 3000}
zfill = matching.plan_fill("buy", 100, zbar, prev_close=10.0, limit=0.1, symbol="sh600000")
print("plan_fill 开盘涨停 ->", zfill)
if not zfill.reason:
    bad("开盘涨停的市价买应判废，实际 %s" % zfill)

# 6) 「既有失败与股票无关」的取证
#    不能用「失败文件都不 import stock」当论据：test_prompt_budget.py 第 8 行
#    `from app import httpd` 会间接导入 app.stock（httpd 启动时 from app import stock）。
#    真正该验的是股票内容有没有进入那段有字数预算的提示词。
KNOWN_FAIL = ["tests/test_memory.py", "tests/test_native_tools.py",
              "tests/test_prompt_budget.py", "tests/test_reader.py"]
print("\n[6] 既有失败与股票的相关性")
for rel in KNOWN_FAIL:
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    tree = ast.parse(src, filename=rel)
    direct = indirect = False
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.ImportFrom) and node.module:
            # `from app import httpd` 的 node.module 只有 "app"，名字在 names 里，
            # 只取 module 会把这种间接导入漏掉（曾因此把 test_prompt_budget 误判成「无引用」）。
            mods.extend(node.module + "." + a.name for a in node.names)
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
        for m in mods:
            if m.startswith("app.stock"):
                direct = True
            elif m == "app.httpd":
                indirect = True
    flag = "直接引用" if direct else ("经 httpd 间接" if indirect else "无引用")
    print("  %-28s %s" % (rel, flag))

from app import agent, mem_tool  # noqa: E402

mem_tool.get_profile_kb = lambda: ""
mem_tool.load_profile = lambda: {"auto_learn_enabled": False}
_nodes = [{"id": "n%d" % i, "title": "条目标题 %d" % i,
           "content": "这是一段比较长的正文，用来模拟真实记忆条目。" * 2}
          for i in range(30)]
_sess = {"agent_enabled": True, "sandbox": ROOT, "ai_write": True,
         "memory": {"nodes": _nodes, "edges": []},
         "messages": [{"role": "user" if i % 2 == 0 else "assistant",
                       "content": "第 %d 条" % i} for i in range(3)]}
_prompt = agent.build_agent_prompt(_sess, search_ready=True, native_tools=True,
                                   cap_domains=set())
_marks = ["stock", "stockkit", "模拟股市", "限价", "公司行为", "风险指标",
          "报告导出", "对局", "持仓", "plan_fill", "risk_metrics",
          "export_report", "adjusted_bars", "corporate_actions"]
_hit = {k: _prompt.count(k) for k in _marks if _prompt.count(k)}
print("  agent 提示 %d 字，命中股票关键词：%s" % (len(_prompt), _hit or "无"))
if _hit:
    bad("股票内容已进入提示词预算，超支不能归为既有问题：%s" % _hit)

print("\n结果：%s" % ("全部通过" if not FAIL else "%d 项失败" % FAIL))
sys.exit(1 if FAIL else 0)
