# -*- coding: utf-8 -*-

from app import computer



def _t_computer_open(root, args, allow_outside=False):
    return computer.dispatch("computer_open", args, root), None


def _t_computer_see(root, args, allow_outside=False):
    return computer.dispatch("computer_see", args, root), None


def _t_computer_act(root, args, allow_outside=False):
    return computer.dispatch("computer_act", args, root), None


def _t_computer_read(root, args, allow_outside=False):
    return computer.dispatch("computer_read", args, root), None


def _t_computer_shot(root, args, allow_outside=False):
    return computer.dispatch("computer_shot", args, root), None


def _t_computer_windows(root, args, allow_outside=False):
    return computer.dispatch("computer_windows", args, root), None


def _computer_text(name, args):
    """Computer Use 的动作摘要（审批卡与工具卡共用）。"""
    args = args if isinstance(args, dict) else {}
    if name == "computer_open":
        return "打开网页：%s" % (args.get("url") or "（未指定）")
    if name == "computer_see":
        return "查看页面元素"
    if name == "computer_act":
        act = args.get("action") or "?"
        ref = args.get("ref")
        val = args.get("value")
        tail = ""
        if act == "type":
            tail = " 输入 %d 字" % len(str(val or ""))
        elif act == "press":
            tail = " 按键 %s" % (val or "Enter")
        elif act == "scroll":
            tail = " %s 像素" % (val or 600)
        return "操作页面：%s%s%s" % (act, (" [%s]" % ref) if ref else "", tail)
    if name == "computer_read":
        return "读取页面：%s" % (args.get("kind") or "console")
    if name == "computer_shot":
        return "截图并让视觉模型描述当前页面"
    if name == "computer_windows":
        return "列出可操作的桌面窗口"
    return name


def judge_computer(name, args, sandbox=None, unrestricted=False):
    """Computer Use 的审批预判：站点授权 / 危险动作。

    返回 (decision, reason, host)。拿不准时按「问一下」处理，不放行。
    unrestricted（完全放权）：不再要求站点白名单、高危动作也不再问；
    但金融 / 密码类内置禁区、设置里的禁止站点、本项目界面仍然拒绝。
    """
    try:
        return computer.judge(name, args, sandbox, unrestricted=unrestricted)
    except Exception as e:
        return "ask", "无法判定这次页面操作的风险：%s" % e, ""
