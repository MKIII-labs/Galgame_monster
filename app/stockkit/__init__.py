# -*- coding: utf-8 -*-
"""模拟股市的分层实现包。

分层（依赖只能自上而下，禁止反向 import）：

    errors      领域异常，最底层，无依赖
    rules       交易规则纯函数：代码规范化 / 手数 / 涨跌停 / 费用
    quotes      行情读取：腾讯为主、东财兜底、Binance 币市
    matching    撮合内核：限价 / 市价、部分成交、涨跌停与停牌判定
    corporate   公司行为：分红、送转（拆股）、停牌
    risk        组合风险指标：最大回撤 / 年化波动 / 胜率 / 超额收益
    report      对局报告导出：Markdown / CSV

`app/stock.py` 仍是唯一的对外门面（对局、数据库、AI 决策都在那里），
它 re-export 本包的公开符号，所以老调用方 `stock.normalize_symbol(...)`
这类写法完全不受影响。

本文件刻意不做任何 import：包初始化必须无副作用，否则会拖慢启动、
也会让测试里的 `stock.configure()` 之前的导入付出额外代价。
"""

__all__ = [
    "errors", "rules", "quotes", "matching", "corporate", "risk", "report",
]
