# -*- coding: utf-8 -*-
"""自动化提问策略：遇到提问卡 / 正文决策问句时，按三档策略处理。

三档（对齐 OpenHands 的 confirmation policy、Cline 的 auto-approve）：

- ``auto``  ：一律自决。可逆的选择直接定；不可逆的操作也不执行，改为
  选一个可回滚的最小方案，并在正文里说明假设。
- ``smart`` ：可逆 / 低风险自决；不可逆（删除、覆盖、发布、付款、改凭据）
  或确实缺少外部信息时，停下来等用户。
- ``ask``   ：一律把问题摆给用户，保留原来的提问卡行为。

这里是纯策略层：只做分类与「替用户回答」，不碰会话、不碰目标状态。
调用方（app/api/chat.py、app/autopilot.py）负责把决策写进目标记录。
"""
import json
import re

from app import llm
from app.config import get_api_conf

MODES = ("auto", "smart", "ask")
DEFAULT_MODE = "smart"
ANSWER_MAX_CHARS = 400

# 不可逆 / 高风险：动钱、动凭据、动别人的东西、覆盖已有成果。
_HIGH_RISK_RE = re.compile(
    r"删除|删掉|移除|清空|格式化|"
    r"覆盖(?:原|已有|文件|数据|内容|掉)|"
    r"重置(?:数据库|配置|环境|系统|仓库|全部)|"
    r"回滚|撤销|不可逆|强制(?:执行|覆盖|推送|删除)|"
    r"发布|上线|部署|投产|生产环境|"
    r"付款|支付|购买|下单|充值|转账|退款|"
    r"api[_ ]?key|密钥|密码|凭据|访问令牌|授权|提升权限|"
    r"rm\s+-rf|drop\s+table|truncate\s+table|"
    r"git\s+(?:reset\s+--hard|push\s+--force|clean\s+-[a-z]*f)",
    re.I)

# 确实缺少外部信息：不是「选哪个好」，而是「没有这条就做不了」。
_MISSING_INFO_RE = re.compile(
    r"(?:需要|请|麻烦).{0,10}(?:提供|发给我|给我|上传|填写|补充)"
    r"|(?:你的|真实的|外部的|生产环境的).{0,10}"
    r"(?:账号|密钥|密码|访问令牌|地址|链接|文件|数据|素材|权限)"
    r"|(?:缺少|没有拿到|拿不到|无法获取|获取不到).{0,12}"
    r"(?:密钥|账号|链接|文件|数据|素材|权限|题面|题干)"
    r"|(?:需要|请|麻烦|必须).{0,10}(?:登录|扫码|验证码)"
    r"|(?:登录|扫码|验证码).{0,8}(?:账号|密码|手机号|邮箱)"
    r"|(?:哪个|哪台|哪一台).{0,6}(?:账号|环境|服务器|仓库|分支|链接|地址)",
    re.I)

# 偏好类：可以按目标自行选一个合理默认值，选错了也能改。
_PREFERENCE_RE = re.compile(
    r"风格|配色|布局|命名|主题|文案|语气|语言|框架|库|技术栈|方案|版本|"
    r"尺寸|颜色|字体|图标|交互|动画|难度|范围|优先级|顺序|格式",
    re.I)


def normalize_mode(value):
    """把配置 / 请求里的策略值规整成三档之一；非法值回落 smart。"""
    s = str(value or "").strip().lower()
    return s if s in MODES else DEFAULT_MODE


def classify(question, options=None):
    """判断一个问题属于哪类风险，返回 ``(risk, why)``。

    risk 三档：``high``（不可逆）/ ``info``（缺外部信息）/ ``low``（可自决）。
    先判高风险，再判缺信息；带明确选项且只涉及偏好的，按低风险处理。
    """
    q = str(question or "").strip()
    opts = [str(x) for x in (options or []) if str(x).strip()]
    text = q + "\n" + "\n".join(opts)
    if not text.strip():
        return "low", "问题为空，按目标继续"
    if _HIGH_RISK_RE.search(text):
        return "high", "涉及删除 / 覆盖 / 发布 / 付款 / 凭据等不可逆操作"
    if _MISSING_INFO_RE.search(text):
        return "info", "缺少外部信息，无法替用户编造"
    if _PREFERENCE_RE.search(text):
        return "low", "偏好类选择，可按目标取合理默认值"
    return "low", "可逆 / 低风险"


def decide(question, options=None, context="", mode="smart",
           goal="", constraints=None):
    """按策略给出处理方式：``{"action": "auto|ask", "answer", "why", "risk"}``。

    ``auto`` 返回可直接回灌给模型的答案；``ask`` 返回空答案，调用方去弹卡 / 暂停。
    """
    mode = normalize_mode(mode)
    risk, why = classify(question, options)
    if mode == "ask":
        return {"action": "ask", "answer": "", "why": why, "risk": risk}
    if risk == "high":
        if mode == "smart":
            return {"action": "ask", "answer": "", "why": why, "risk": risk}
        # auto：也不做不可逆操作，改为可回滚的最小方案
        return {"action": "auto",
                "answer": ("不要执行删除 / 覆盖 / 发布 / 付款 / 改凭据这类不可逆操作；"
                           "选择可回滚的最小方案，先做一步可验证的改动，并在正文里说明假设。"),
                "why": "自动模式下也避开不可逆操作", "risk": risk}
    if risk == "info" and mode == "smart":
        return {"action": "ask", "answer": "", "why": why, "risk": risk}
    answer = _llm_answer(question, options, context, goal, constraints)
    return {"action": "auto", "answer": answer,
            "why": why if risk == "low" else (why + "；先按最小可逆方案推进"),
            "risk": risk}


def _llm_answer(question, options, context, goal, constraints):
    """让轻量模型替用户选一个最合理的答案；失败时退回确定性默认值。"""
    opts = [str(x) for x in (options or []) if str(x).strip()]
    api_key, api_url, api_model = get_api_conf("chat")
    fallback = _fallback_answer(opts)
    if not api_key:
        return fallback
    ctx = str(context or "")[-3000:]
    cons = [str(x) for x in (constraints or []) if str(x).strip()][:8]
    prompt = "\n".join([
        "你在代替用户回答一个自动化任务里的问题。只输出 JSON，不要解释。",
        "",
        "## 目标",
        str(goal or "").strip() or "（见上下文）",
        "",
        "## 用户约束",
        ("\n".join("- " + c for c in cons) or "（无）"),
        "",
        "## 当前上下文",
        ctx or "（无）",
        "",
        "## 问题",
        str(question or "").strip(),
        "",
        "## 选项",
        ("\n".join("- " + o for o in opts) or "（无，自由回答）"),
        "",
        '输出：{"answer":"你的回答","why":"一句话依据"}',
        "规则：优先可逆、可回滚、成本低的选项；不确定时选能先做小步验证的那个；"
        "不要编造外部事实；答案尽量短。",
    ])
    try:
        out = llm._quick_chat(
            api_key, api_url, api_model,
            [{"role": "user", "content": prompt}],
            max_tokens=300, source="autonomy") or ""
    except Exception:
        return fallback
    data = _json_loose(out)
    ans = str(data.get("answer") or "").strip()
    if not ans:
        return fallback
    if opts and ans not in opts:
        # 模型没按选项回答时，至少把它往选项上靠；靠不上就保留原文
        hit = next((o for o in opts if o and o in ans), "")
        ans = hit or ans
    return ans[:ANSWER_MAX_CHARS]


def _fallback_answer(options):
    if options:
        return options[0]
    return "按目标继续，采用最小可逆改动；完成后说明本次假设。"


def _json_loose(text):
    s = str(text or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        data = json.loads(s[i:j + 1])
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
