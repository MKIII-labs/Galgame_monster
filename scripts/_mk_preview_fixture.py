"""把 tests/fixtures/stock_ui.json 内联成 web/_stock_fixture.js。

file:// 下 fetch 本地 JSON 会被跨域拦掉，改成 <script> 直接赋全局变量最稳。
产物是临时自检文件，验完可删。
"""
import io
import json

SRC = "tests/fixtures/stock_ui.json"
DST = "web/_stock_fixture.js"

data = json.load(io.open(SRC, encoding="utf-8"))
# risk 为空时给一份形状正确的占位，保证风险面板能渲染出东西而不是空白
if not data.get("risk"):
    data["risk"] = {
        "max_drawdown": 6.42, "volatility": 1.87, "sharpe": 0.34,
        "win_rate": 33.3, "profit_factor": 1.42, "closed_trades": 3, "days": 12,
        "best_trade": 812.5, "worst_trade": -430.2, "total_return": 3.21,
    }

body = "window.__FIXTURE__ = " + json.dumps(data, ensure_ascii=False) + ";\n"
io.open(DST, "w", encoding="utf-8").write(body)
print("fixture js ->", DST, len(body), "bytes")
