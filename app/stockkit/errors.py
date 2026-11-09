# -*- coding: utf-8 -*-
"""模拟股市的领域异常。

独立成模块的原因：它是最底层，rules / quotes / matching / corporate 都要能
`from app.stockkit.errors import StockError`，而不必反向依赖 `app.stock`。
反向依赖会在 `app.stock` 导入本包时形成循环。
"""


class StockError(Exception):
    """可读的业务错误（行情失败、订单非法、对局不存在等）。

    约定：所有对外函数都不抛网络库 / sqlite 的原始异常，
    统一转成这个消息可读、能直接显示给用户的类型。
    """
