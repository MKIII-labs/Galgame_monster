# -*- coding: utf-8 -*-
"""Token 预算的唯一事实源（批 1：只读观测）。

背景：预算口径原本散落在 `context.context_limits` / `request_budget` /
`effective_output_cap` 与 `api/chat.py` 内联的 `_req_budget` 之间，彼此换算。
这里把它们收拢成一份不可变快照，写进用量流水，供诊断与后续批次共用。

**本模块不参与任何裁剪 / 压缩决策**：所有数值都委托既有函数计算，
因此与线上实际发送逐值一致（`tests/test_token_budget.py` 锁定）。
"""

from dataclasses import dataclass

from app import context

# 分级阈值（占可用提示词预算的比例）：折叠 → 轻压缩 → 结构化压缩。
# 批 1 只用于展示「这一轮落在哪一级」，不触发任何动作。
L1_FOLD = 0.35
L2_MICRO = 0.60
L3_COMPACT = 0.85


@dataclass(frozen=True)
class Budget:
    """一次请求的预算快照。字段含义与 `context` 里的计算保持一致。"""

    window: int          # 用户设定的上下文窗口（0 = 不限）
    reserve: int         # 为输出预留
    headroom: int        # 估算误差余量
    output_cap: int      # 单次输出上限（被窗口比例夹紧后的值）
    prompt_cap: int      # 可用提示词预算

    @property
    def unlimited(self):
        return self.window <= 0 or self.prompt_cap <= 0

    @property
    def l1_fold(self):
        return int(self.prompt_cap * L1_FOLD)

    @property
    def l2_micro(self):
        return int(self.prompt_cap * L2_MICRO)

    @property
    def l3_compact(self):
        return int(self.prompt_cap * L3_COMPACT)

    def as_dict(self):
        return {
            "window": self.window,
            "reserve": self.reserve,
            "headroom": self.headroom,
            "output_cap": self.output_cap,
            "prompt_cap": self.prompt_cap,
            "l1_fold": self.l1_fold,
            "l2_micro": self.l2_micro,
            "l3_compact": self.l3_compact,
        }


def budget(model="", cfg=None):
    """当前配置下的预算快照。任何异常都回落到「不限」，不会影响对话。"""
    try:
        cfg = cfg if cfg is not None else context.load_config()
        window, reserve = context.context_limits(model)
        if window <= 0:
            return Budget(0, reserve, 0, 0, 0)
        try:
            headroom = int(cfg.get("chat_output_headroom") or 0)
        except (TypeError, ValueError):
            headroom = 0
        return Budget(window=window, reserve=reserve, headroom=headroom,
                      output_cap=context.effective_output_cap(window, cfg),
                      prompt_cap=context.request_budget(model, cfg))
    except Exception:                    # noqa: BLE001  观测失败不能拦住请求
        return Budget(0, 0, 0, 0, 0)


def level(used, b):
    """这一轮落在哪一级：0 = 正常，1 = 该折叠，2 = 该轻压缩，3 = 该结构化压缩。"""
    if not isinstance(b, Budget) or b.unlimited or used <= 0:
        return 0
    if used >= b.l3_compact:
        return 3
    if used >= b.l2_micro:
        return 2
    if used >= b.l1_fold:
        return 1
    return 0
