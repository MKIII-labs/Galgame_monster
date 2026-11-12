# -*- coding: utf-8 -*-

import fnmatch
import json
import os
import threading
import time
import uuid

from .tools_computer import _computer_text


APPROVAL_TIMEOUT = 600                     # 等待用户批准写入的超时（秒）
# 写入审批档位（对齐 Codex）：
#   ask    手动审批：每次写入前都让用户确认
#   ai     自动审批：本地规则 + 风险启发式，只有高风险动作才问
#   full   完全访问：不问，直接写入；命令也不再受白名单 / 内联 / 管道限制
#          （沙箱边界、金融禁区仍保留）
#   custom 自定义：只按沙箱内 .gm/agent-rules.json 的规则判定


APPROVAL_MODES = ("ask", "ai", "full", "custom")


APPROVAL_DEFAULT = "ai"
# auto / always 是旧默认值，语义就是「自动审批」；once 仍按手动处理。


APPROVAL_ALIASES = {"auto": "ai", "always": "ai", "once": "ask"}


SECRET_NAMES = {
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "credentials", "credentials.json", "secrets.json", "secrets.yaml",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "service-account.json",
}


SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ppk")


SECRET_SEGMENTS = (".ssh", ".aws", ".gnupg", ".kube", ".docker")


def is_secret_path(rel_or_abs):
    """这个路径是不是密钥 / 凭据类文件？返回命中的规则（空串 = 不是）。

    公开密钥（.pub）不算秘密；只看文件名 / 路径段，不做内容判断。
    """
    s = str(rel_or_abs or "").replace("\\", "/").strip().lower()
    if not s:
        return ""
    parts = [p for p in s.split("/") if p and p not in (".", "..")]
    if not parts:
        return ""
    for seg in parts[:-1]:
        if seg in SECRET_SEGMENTS:
            return seg + "/"
    name = parts[-1]
    if name.endswith(".pub"):              # id_rsa.pub 是公钥，允许读
        return ""
    if name in SECRET_NAMES:
        return name
    if name == ".env" or name.startswith(".env."):
        return ".env"
    for suf in SECRET_SUFFIXES:
        if name.endswith(suf):
            return "*" + suf
    return ""


def _secret_allowed(sandbox, rels):
    """规则文件里显式 allow 过的秘密文件才放行（deny / ask 都不算放行）。"""
    from .rules import load_effective_rules
    try:
        rules, err = load_effective_rules(sandbox)
        if err:
            return False
        hit, _pat = _match_rules(rules, rels)
        return hit == "allow"
    except Exception:
        return False


def _secret_deny_text(pattern):
    return ("❌ 这是密钥 / 凭据类文件（命中 %s），默认拒绝读取。"
            "需要时请让用户在 .gm/agent-rules.json 的 allow 里显式放行，"
            "或由用户手动把内容贴给你。" % pattern)

# 「看一眼图片」的边界：太大直接拒，避免十几 MB 的 base64 撑爆请求


_APPROVALS = {}


_APPROVE_REMEMBER = {}          # aid -> True（用户点了「本轮都允许」）


_approvals_lock = threading.Lock()


def normalize_approval(mode):
    """把会话里存的档位收敛成四种之一；旧值（auto/once/always）自动映射。"""
    m = str(mode or "").strip().lower()
    m = APPROVAL_ALIASES.get(m, m)
    return m if m in APPROVAL_MODES else APPROVAL_DEFAULT


def _match_rules(rules, rels):
    """按 deny > ask > allow 的优先级匹配规则，返回 (档位, 命中的 pattern)。"""
    for kind in ("deny", "ask", "allow"):
        for pat in rules.get(kind) or []:
            for rel in rels:
                if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(rel), pat):
                    return kind, pat
    return "", ""


def judge_write(sandbox, name, args, mode):
    """判断一次写入要不要先问用户。

    返回 (decision, reason)，decision ∈ {"allow", "ask", "deny"}。
    规则文件在 ai / custom 两档都生效：deny 永远拦、allow 免问、ask 必问。

    ai 档按**风险**判定，不再按「本轮写了第几次」：
    - 低 / 中风险直接放行：新建文件、建目录、普通编辑（不管改多少行）
    - 高风险才问：删除、移动 / 重命名、语义重命名、覆盖已有文件
      （不可逆，或一次动到别处看不见的地方）
    """
    from .rules import load_effective_rules
    from .sandbox import (SandboxError, _write_rel_paths, resolve_path)
    mode = normalize_approval(mode)
    if mode == "full":
        return "allow", ""
    if mode == "ask":
        return "ask", "手动审批：写入前需你确认"
    args = args if isinstance(args, dict) else {}
    rels = _write_rel_paths(sandbox, name, args)
    rules, err = load_effective_rules(sandbox)
    if err:
        return "ask", err
    hit, pat = _match_rules(rules, rels)
    if hit == "deny":
        return "deny", "规则禁止：%s" % pat
    if hit == "ask":
        return "ask", "规则要求确认：%s" % pat
    if hit == "allow":
        return "allow", ""
    if mode == "custom":
        return "ask", "自定义规则未覆盖：%s" % (rels[0] if rels else name)
    # mode == "ai"：只拦高风险动作
    if name == "delete_file":
        return "ask", "删除不可撤销（回滚也可能因快照过期失效）"
    if name == "move_file":
        return "ask", "移动 / 重命名会改变其他地方对它的引用"
    if name == "lsp_rename":
        # 语义重命名会同时改多个文件，ai 档也不能自行放行
        return "ask", "语义重命名会一次改动多个文件"
    if name == "write_file":
        try:
            p = resolve_path(sandbox, args.get("path"))
        except SandboxError:
            return "allow", ""          # 路径本身会被工具拒绝，不必再问
        if os.path.exists(p):
            return "ask", "会整个覆盖已有文件（原内容被整体替换），确认是否继续"
    return "allow", ""


def describe_call(name, args):
    """零成本的本地摘要：这次操作是什么、风险多高（供审批卡「先看一眼」）。

    只是把参数翻译成人话，不做任何判定、不发请求。
    """
    from .groups import (COMPUTER_TOOLS, WRITE_TOOLS)
    from .sandbox import clean_cwd
    from .tools_exec import (_cmd_argv, _cmd_kind)
    from .undo import _WRITE_LABEL
    args = args if isinstance(args, dict) else {}
    if name == "run_command":
        argv, err = _cmd_argv(args)
        if err:
            return {"kind": "command", "risk": "高风险", "argv": [],
                    "text": "命令无法解析：%s" % err}
        kind = _cmd_kind(argv)
        risk = {"readonly": "只读", "normal": "一般", "high": "高风险"}[kind]
        cwd = clean_cwd(args.get("cwd"))
        return {"kind": "command", "risk": risk, "argv": argv,
                "text": "执行 %s（工作目录 %s，风险 %s）"
                        % (" ".join(argv), cwd, risk)}
    if name == "render_check":
        return {"kind": "read", "risk": "只读",
                "text": "渲染自检：%s（视口 %s%s）"
                        % (args.get("path") or "（未指定路径）",
                           args.get("viewport") or "1280x800",
                           "，含截图描述" if args.get("screenshot") else "")}
    if name == "audio_probe":
        return {"kind": "read", "risk": "只读",
                "text": "音频自检：%s" % (args.get("path") or "（未指定路径）")}
    if name == "audio_compare":
        return {"kind": "read", "risk": "只读",
                "text": "音频对比：%s → %s" % (args.get("a") or "？", args.get("b") or "？")}
    if name == "see_image":
        return {"kind": "read", "risk": "只读",
                "text": "看图：%s" % (args.get("path") or "（未指定路径）")}
    if name == "read_tool_result":
        return {"kind": "read", "risk": "只读",
                "text": "精读结果 %s（第 %s–%s 字）" % (
                    args.get("handle") or "？", args.get("start") or 0,
                    args.get("end") or "…")}
    if name == "find_symbol":
        return {"kind": "read", "risk": "只读",
                "text": "查符号：%s" % (args.get("name") or "（未指定）")}
    if name == "check_code":
        return {"kind": "read", "risk": "只读",
                "text": "语法体检：%s" % (args.get("path") or "沙箱根目录")}
    if name == "lsp_diagnostics":
        return {"kind": "read", "risk": "只读",
                "text": "类型检查：%s" % (args.get("path") or "沙箱根目录")}
    if name == "lsp_definition":
        return {"kind": "read", "risk": "只读",
                "text": "跳转定义：%s:%s" % (args.get("path") or "？", args.get("line") or "？")}
    if name == "lsp_references":
        return {"kind": "read", "risk": "只读",
                "text": "查找引用：%s" % (args.get("symbol") or args.get("path") or "？")}
    if name == "lsp_rename":
        return {"kind": "write", "risk": "写入",
                "text": "重命名 %s → %s" % (args.get("symbol") or "？",
                                            args.get("new_name") or "？")}
    if name == "git_status":
        return {"kind": "read", "risk": "只读", "text": "查看 Git 状态"}
    if name == "git_diff":
        return {"kind": "read", "risk": "只读",
                "text": "查看改动：%s" % (args.get("path") or "？")}
    if name == "git_log":
        return {"kind": "read", "risk": "只读", "text": "查看提交历史"}
    if name == "git_commit":
        msg = str(args.get("message") or "").strip().replace("\n", " ")[:60]
        return {"kind": "write", "risk": "写入", "text": "Git 提交：%s" % (msg or "（无提交信息）")}
    if name in COMPUTER_TOOLS:
        return {"kind": "computer", "risk": "其他", "text": _computer_text(name, args)}
    if name in WRITE_TOOLS:
        path = str(args.get("path") or args.get("src") or args.get("dst") or "").strip()
        return {"kind": "write",
                "risk": "高风险" if name == "delete_file" else "写入",
                "text": "%s：%s" % (_WRITE_LABEL.get(name, name), path or "（未指定路径）")}
    return {"kind": "tool", "risk": "其他", "text": name}


def _match_command_rules(rules, text):
    """命令规则：pattern 用 glob 匹配整条命令（如 "python -m pytest*"）。"""
    cmds = (rules or {}).get("commands") or {}
    low = str(text or "").lower()
    for kind in ("deny", "ask", "allow"):
        for pat in cmds.get(kind) or []:
            if fnmatch.fnmatch(low, pat.lower()):
                return kind, pat
    return "", ""


def _tool_arg_brief(name, args):
    """工具规则用的参数摘要：命令取整条，文件类取路径，其余取紧凑 JSON。

    规则里的 `run_command:git push*` 就是拿这个摘要去匹配的。
    """
    args = args if isinstance(args, dict) else {}
    if name == "run_command":
        argv = args.get("argv")
        if isinstance(argv, list):
            return " ".join(str(x) for x in argv)
        return str(args.get("cmd") or args.get("command") or "")
    for key in ("path", "to", "from", "query", "server", "tool", "url"):
        if args.get(key):
            return str(args[key])
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def _match_tool_rules(rules, name, args):
    """按 deny > ask > allow 匹配工具级规则，返回 (档位, 命中的 pattern)。

    语法两种：
    - `mcp__fs__read_*`      只匹配工具名（glob）
    - `run_command:git push*` 工具名 + 参数摘要（glob），冒号后的部分为空则等价于只匹配工具名
    """
    nm = str(name or "").strip().lower()
    brief = _tool_arg_brief(name, args).strip().lower()
    tools = (rules or {}).get("tools") or {}
    for kind in ("deny", "ask", "allow"):
        for pat in tools.get(kind) or []:
            p = str(pat or "").strip()
            if not p:
                continue
            tname, sep, targ = p.partition(":")
            if not fnmatch.fnmatch(nm, tname.strip().lower()):
                continue
            targ = targ.strip().lower()
            if sep and targ and not fnmatch.fnmatch(brief, targ):
                continue
            return kind, pat
    return "", ""


def judge_tool_rule(sandbox, name, args):
    """只查工具级规则；返回 (decision, reason)，decision 为空表示未命中。

    deny 是硬拦截，连「本轮都允许」也绕不过（与路径规则一致）。
    """
    from .rules import load_effective_rules
    rules, err = load_effective_rules(sandbox)
    if err:
        return "", ""
    kind, pat = _match_tool_rules(rules, name, args)
    if kind == "deny":
        return "deny", "规则禁止：%s" % pat
    if kind == "ask":
        return "ask", "规则要求确认：%s" % pat
    if kind == "allow":
        return "allow", ""
    return "", ""


def requires_approval(name):
    """没有专门风险判定器的工具（MCP / 插件）默认要问一次。

    它们能碰到的东西不由本进程控制，宁可多问一次，也不默认放行。
    MCP 工具可以在 server 配置里用 readonly_tools（glob）声明为只读，免审批。
    """
    from .groups import _PLUGIN_TOOLS
    n = str(name or "").strip()
    if n.startswith("mcp__"):
        try:
            from app import mcp
            return not mcp.is_readonly_tool(n)
        except Exception:                     # noqa: BLE001  配置坏了就当它要审批
            return True
    meta = _PLUGIN_TOOLS.get(n)
    if meta is not None:
        return str(meta.get("permission") or "write") != "read"
    return False


def judge_command(sandbox, args, mode, shell_mode="strict", unrestricted=False,
                  interactive=True):
    """判断一条命令要不要先问用户。返回 (decision, reason)。

    ai 档按**风险**判定，不按本轮执行了几次：只读与普通命令直接放行，
    安装 / 联网 / 破坏性 Git 才问。

    unrestricted 由上层按「完全放权」档位传入：白名单 / 内联执行 / 管道限制
    一律不查。沙箱边界仍由执行入口（`_t_run_command` 的 cwd）把关。

    interactive=False（无人值守）：高危 git 命令直接拒绝，而不是弹一个没人看的审批。
    """
    from .rules import load_effective_rules
    from .tools_exec import (_cmd_kind, _cmd_plan, _cmd_plan_unrestricted)
    from .tools_git import _dangerous_git
    mode = normalize_approval(mode)
    if unrestricted or mode == "full":
        # 只校验「有东西可跑」；空命令仍按非法参数拒绝
        _plan, _err = _cmd_plan_unrestricted(args)
        if _err:
            return "deny", _err
        if _dangerous_git(_plan):
            # 覆盖整个工作区 / 改写历史的命令：完全放权也要问
            return (("ask", "高危 git 命令（可能覆盖工作区或改写历史），需你确认")
                    if interactive else
                    ("deny", "高危 git 命令在无人值守模式下被拒绝"))
        return "allow", ""
    plan, err = _cmd_plan(args, sandbox, shell_mode)
    if err:
        return "deny", err
    if mode == "ask":
        return "ask", "手动审批：执行命令前需你确认"
    text = plan["cmd"] or " ".join(plan["argv"])
    rules, rerr = load_effective_rules(sandbox)
    if rerr:
        return "ask", rerr
    hit, pat = _match_command_rules(rules, text)
    if hit == "deny":
        return "deny", "规则禁止：%s" % pat
    if hit == "ask":
        return "ask", "规则要求确认：%s" % pat
    if hit == "allow":
        return "allow", ""
    if mode == "custom":
        return "ask", "自定义规则未覆盖该命令：%s" % text
    if plan["shell"]:
        # 管道取「最危险那一段」：一段是 pip install，整条就该问
        kind = "readonly"
        for seg in plan["segments"]:
            k = _cmd_kind(seg["argv"])
            if k == "high":
                kind = "high"
                break
            if k == "normal":
                kind = "normal"
    else:
        kind = _cmd_kind(plan["argv"])
    if kind == "readonly":
        return "allow", ""
    if kind == "high":
        return "ask", "安装 / 联网 / 改仓库历史类命令，需人工确认"
    return "allow", ""


def judge_shell(mode, unrestricted=False, interactive=True):
    """持久 shell 的审批预判。

    它等于「任意命令执行」，没法像 run_command 那样按可执行白名单 / 风险分类，
    所以沿用 Agent 的现有分档，但把基线当作高危：
      - full / 完全放权 → 放行
      - ask            → 每次问
      - ai             → 视为高危，问一次
      - custom         → 由 .gm/agent-rules.json 的 tools 段决定；未命中默认问
    工具级规则（deny/allow/ask）在调用方已经先跑过，这里只处理兜底。
    """
    if unrestricted:
        return "allow", ""
    m = normalize_approval(mode)
    if m == "full":
        return "allow", ""
    if not interactive:
        return "deny", "无人值守模式下不自动执行持久 shell"
    if m == "ask":
        return "ask", "手动审批：持久 shell 可执行任意命令，需你确认"
    return "ask", "持久 shell 属于高危操作（任意命令），需要确认"


def _approval_notice(tool, args):
    """审批通知的一行摘要：直接复用审批卡上的「先看一眼」。"""
    a = args if isinstance(args, dict) else {}
    if tool == "apply_worktree":
        return "回写子代理改动：%s" % (a.get("summary") or a.get("agent") or "")
    try:
        return describe_call(tool, a).get("text") or str(tool or "")
    except Exception:                        # noqa: BLE001  摘要失败不能挡住审批本身
        return str(tool or "")


def new_approval(sid, tool, args):
    aid = "p_" + uuid.uuid4().hex[:10]
    with _approvals_lock:
        _APPROVALS[aid] = {"event": threading.Event(), "ok": None, "sid": sid,
                           "tool": tool, "args": args, "remember": False,
                           "ts": time.time()}
    from app import session_status
    session_status.mark(sid, "approval", _approval_notice(tool, args))
    return aid


def resolve_approval(aid, ok, remember=False):
    with _approvals_lock:
        item = _APPROVALS.get(aid)
    if not item:
        return False
    item["ok"] = bool(ok)
    item["remember"] = bool(remember and ok)
    item["event"].set()
    return True


def get_approval(aid):
    """只读地取一条待审批记录，供「提问」使用。不 pop、不 resolve，审批照常等待。"""
    with _approvals_lock:
        item = _APPROVALS.get(aid)
    if not item:
        return None
    return {"tool": item.get("tool") or "", "args": item.get("args") or {},
            "sid": item.get("sid") or ""}


def approval_remember(aid):
    """取出并清除「本轮都允许」标记（必须在 wait_approval 之后调用）。"""
    with _approvals_lock:
        return bool(_APPROVE_REMEMBER.pop(aid, False))


def remember_approval_rule(sandbox, name, args):
    """「始终允许」：把这一条写成项目规则（`.gm/agent-rules.json`），返回 (ok, note)。

    只写最窄的匹配，绝不写成工具级通配（那等于把所有同类调用一次放开）：
    - run_command → `commands.allow` 里放整条命令
    - 文件类工具 → `tools.allow` 里放 `工具名:相对路径`
    - 其它工具   → `tools.allow` 里放 `工具名:参数摘要`
    写入是「读改写 + 临时文件替换」，不动规则文件里已有的其它字段。
    """
    from .groups import (GIT_WRITE_TOOLS, WRITE_TOOLS)
    from .rules import rules_path
    from .sandbox import (_write_rel_paths, clean_sandbox)
    from .tools_exec import (_cmd_kind, _cmd_plan)
    from .tools_git import _dangerous_git
    root = clean_sandbox(sandbox)
    if not root:
        return False, "没有沙箱目录，无法写项目规则"
    path = rules_path(root)
    raw = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            return False, "现有规则文件读不出来，已跳过写入：%s" % e
        if not isinstance(raw, dict):
            return False, "现有规则文件不是 JSON 对象，已跳过写入"
    nm = str(name or "").strip()
    if not nm:
        return False, "缺少工具名"
    if nm == "shell":
        return False, "持久 shell 等于任意命令执行，不支持「始终允许」"
    if nm.startswith("computer_"):
        return False, "浏览器 / 桌面操作不支持「始终允许」"
    if nm in WRITE_TOOLS or nm in GIT_WRITE_TOOLS:
        rels = _write_rel_paths(root, nm, args)
        brief = str(rels[0] if rels else _tool_arg_brief(nm, args)).strip()
        section, rule = "tools", nm + (":" + brief if brief else "")
    elif nm == "run_command":
        brief = _tool_arg_brief(nm, args).strip()
        if not brief:
            return False, "拿不到命令内容"
        # 高风险命令（安装 / 联网 / 改仓库历史 / 管道串联）不给「始终允许」：
        # 这类一次放行等于长期交出安装权或改历史权
        try:
            plan, perr = _cmd_plan(args, root, "strict")
        except Exception:                     # noqa: BLE001
            plan, perr = None, "命令解析失败"
        if perr or not plan:
            return False, "命令解析失败，未写入规则：%s" % (perr or "")
        if plan.get("shell") or _dangerous_git(plan) or _cmd_kind(plan["argv"]) == "high":
            return False, "安装 / 联网 / 改仓库历史类命令不支持「始终允许」"
        # 前缀匹配：`python -m pytest` 记住后，`python -m pytest -q` 也放行
        section = "commands"
        rule = brief if brief.endswith("*") else brief + "*"
    else:
        brief = _tool_arg_brief(nm, args).strip()
        section, rule = "tools", nm + (":" + brief if brief else "")
    sec = raw.get(section)
    if not isinstance(sec, dict):
        sec = {}
        raw[section] = sec
    lst = sec.get("allow")
    if not isinstance(lst, list):
        lst = []
        sec["allow"] = lst
    if rule in lst:
        return True, "这条规则已经存在：%s" % rule
    lst.append(rule)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        return False, "规则写入失败：%s" % e
    return True, "已写入规则：%s" % rule


def wait_approval(aid, timeout=APPROVAL_TIMEOUT, stop_event=None):
    """返回 True=允许 / False=拒绝 / None=超时或被停止。"""
    with _approvals_lock:
        item = _APPROVALS.get(aid)
    if not item:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        if item["event"].wait(0.2):
            break
        if stop_event is not None and stop_event.is_set():
            break
    with _approvals_lock:
        _APPROVALS.pop(aid, None)
        if item.get("remember"):
            _APPROVE_REMEMBER[aid] = True
            if len(_APPROVE_REMEMBER) > 200:      # 防御：异常路径下别无限堆积
                _APPROVE_REMEMBER.clear()
                _APPROVE_REMEMBER[aid] = True
    from app import session_status
    session_status.clear(item.get("sid"), "approval")
    return item["ok"]


# ============================================================
# 3.3b 计划审批（Claude 式 plan mode：计划与执行分离）
# ============================================================
# 计划卡发出后先挂起，等用户选择：
#   approve → 按当前计划开始执行
#   comment → 保持规划模式，让模型按意见重排（重排后会再次挂起）
#   edit    → 用户逐条改过步骤，直接按修改后的计划执行
# 只在「计划第一次出现、还没动过手」时挂起；批准之后同一轮再改计划不再打断。
