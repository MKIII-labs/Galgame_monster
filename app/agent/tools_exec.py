# -*- coding: utf-8 -*-

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from app import winproc
from app.config import load_config



CMD_DEFAULT_TIMEOUT = 60        # 单条命令默认超时（秒）


CMD_MAX_TIMEOUT = 600           # 允许模型指定的最大超时


CMD_MAX_OUTPUT = 20000          # 回灌给模型的输出上限（头 12k + 尾 8k）
# 只允许这些可执行文件；不在名单里的一律拒绝（不是「问一下」，是「跑不了」）


CMD_ALLOW_EXES = {"python", "python3", "py", "node", "nodejs", "npm", "npx",
                  "pnpm", "yarn", "bun", "deno", "pip", "pip3", "git", "pytest", "ruff",
                  # 跨语言构建 / 测试 / 编译：只放行常见工具链，装依赖仍按高风险问
                  "go", "gofmt", "cargo", "rustc", "rustfmt",
                  "java", "javac", "mvn", "mvnw", "gradle", "gradlew", "dotnet",
                  "make", "cmake", "ninja", "meson",
                  "gcc", "g++", "clang", "clang++", "cc", "c++",
                  "tsc", "eslint", "prettier", "vite", "webpack", "rollup",
                  "php", "composer", "ruby", "bundle", "swift", "dart", "flutter",
                  "kotlinc", "scala", "sbt"}
# 这些即使进了白名单也永远拒绝：换壳执行 / 删除 / 联网下载 / 计划任务


CMD_DENY_EXES = {"cmd", "powershell", "pwsh", "bash", "sh", "rm", "del", "erase",
                 "format", "reg", "curl", "wget", "certutil", "mshta",
                 "bitsadmin", "schtasks", "shutdown", "taskkill"}
# 命令串联 / 重定向 / 变量展开：只对**字符串形式**的 cmd 拒绝；
# argv 形式不经过 shell，这些字符就是普通参数，不该误杀（python -c 里常见分号）


CMD_META_CHARS = ("|", "&", ";", ">", "<", "`", "$(", "%COMSPEC%")
# 环境变量清洗：名字命中这些词的一律不传给子进程


CMD_ENV_DROP = re.compile(r"(?i)(key|token|secret|password|passwd|cookie|credential|auth)")
# 只读的 git 子命令；不在里面的（commit/push/reset/clean/checkout…）都算高风险


CMD_GIT_READONLY = {"status", "log", "diff", "show", "rev-parse", "ls-files",
                    "describe", "blame", "shortlog", "whatchanged"}
# 只改本地仓库、不动远端历史：自动审批下按中风险放行


CMD_GIT_MEDIUM = {"add", "commit", "stash", "branch"}


CMD_NPM_HIGH = {"install", "i", "ci", "add", "update", "publish", "link",
                "uninstall", "remove", "rm", "exec", "x", "dlx"}
# 内联执行：不落盘就能跑任意代码，等于绕过上面的可执行白名单。
# 曾经 `python -c "import subprocess; subprocess.run(['msedge',...])"` 在 ai 档直接放行。


CMD_INLINE_FLAGS = {
    "python": ("-c", "-"), "python3": ("-c", "-"), "py": ("-c", "-"),
    "node": ("-e", "-p", "-"), "nodejs": ("-e", "-p", "-"),
}


CMD_INLINE_PREFIX = ("--eval=", "--print=")       # 长写法按前缀判断
# -m 只放行这些模块；其余模块（如 -m http.server）等价于内联代码


CMD_ALLOW_MODULES = {"pytest", "pip", "unittest", "compileall", "json.tool"}
# 解释器要执行的脚本必须落在沙箱内，且扩展名合法


CMD_SCRIPT_EXTS = {"python": (".py",), "python3": (".py",), "py": (".py",),
                   "node": (".js", ".mjs", ".cjs"), "nodejs": (".js", ".mjs", ".cjs")}


CMD_VERSION_FLAGS = {"--version", "-version", "-v", "-V", "-h", "--help"}

# 这些目录永远不进入（噪音 / 版本库 / 虚拟环境）


SHELL_TOOLS = ("shell",)
# 会改变仓库状态、但改不了文件内容的工具：单独一档，审批走 judge_git_commit


def _t_run_workflow_script(root, args, caps=None):
    """脚本化工作流：脚本里可调工具与 DAG 工作流。"""
    from app import workflow_script
    caps = caps or {}
    text, _stat = workflow_script.run(
        root, args.get("code") or "", caps=caps,
        stop_event=caps.get("stop_event"),
        emit=caps.get("emit") or (lambda o: None),
        timeout=args.get("timeout") or 0,
        allow_write=bool(caps.get("code_allow_write")),
        allow_cmd=bool(caps.get("code_allow_cmd")),
        allow_outside=bool(caps.get("allow_outside")))
    return text, None


def _exe_name(raw):
    name = os.path.basename(str(raw or "")).lower()
    return name[:-4] if name.endswith(".exe") else name


def _cmd_inline_error(exe, rest):
    """拦住内联执行（python -c / node -e / 读 stdin）。

    这类命令不经过任何文件就能跑任意代码（包括再起一个白名单外的 exe），
    等于把「可执行白名单」变成摆设。模型要跑代码，就让它先写进沙箱。
    """
    bad = CMD_INLINE_FLAGS.get(exe)
    if not bad:
        return ""
    for tok in rest:
        t = str(tok)
        if t in bad or t.startswith(CMD_INLINE_PREFIX):
            return ("不允许内联执行代码（%s %s）：请把代码写成沙箱内的 %s 文件再运行"
                    % (exe, t, "/".join(CMD_SCRIPT_EXTS.get(exe, (".py",)))))
    return ""


def _cmd_script_error(exe, rest, root):
    """解释器只能跑沙箱内的脚本；-m 只放行固定几个模块。

    root 为 None（仅展示用，如 describe_call）时跳过，真正的执行入口一定带 root。
    """
    from .sandbox import (SandboxError, resolve_path)
    exts = CMD_SCRIPT_EXTS.get(exe)
    if not root or not exts:
        return ""
    # 纯版本 / 帮助查询（python --version、py -3 --version）不需要脚本
    if rest and all(str(t).startswith("-") for t in rest) \
            and any(str(t) in CMD_VERSION_FLAGS for t in rest):
        return ""
    if "-m" in [str(t) for t in rest]:
        i = [str(t) for t in rest].index("-m")
        mod = str(rest[i + 1]) if i + 1 < len(rest) else ""
        if not mod or mod.startswith("-"):
            return "参数 -m 后面缺少模块名"
        if mod not in CMD_ALLOW_MODULES:
            return ("-m 只允许：%s（其它模块等价于内联执行代码）"
                    % "、".join(sorted(CMD_ALLOW_MODULES)))
        return ""
    script = ""
    for tok in rest:
        if not str(tok).startswith("-"):
            script = str(tok)
            break
    if not script:
        return "请给出要执行的脚本（不支持交互式解释器）"
    try:
        p = resolve_path(root, script)
    except SandboxError:
        return "只能执行沙箱内的脚本：%s" % script
    if not os.path.isfile(p):
        return "脚本不存在：%s" % script
    if not p.lower().endswith(exts):
        return "脚本扩展名必须是 %s：%s" % (" / ".join(exts), script)
    return ""


def _cmd_argv(args, root=None):
    """把参数收敛成 argv；不合法时返回 (None, 原因)。

    argv 是首选形式。也兼容 cmd 字符串，但只切词、不交给 shell ——
    含管道 / 重定向 / 串联符号的直接拒绝，避免「切词看起来对、其实执行了别的东西」。

    root 给了沙箱根时，额外校验「解释器跑的是沙箱内脚本、不是内联代码」；
    只用于展示的调用（describe_call）可以不传。
    """
    args = args if isinstance(args, dict) else {}
    raw = args.get("argv")
    argv = []
    from_string = False
    if isinstance(raw, list) and raw:
        argv = [str(x) for x in raw if str(x) != ""]
    else:
        cmd = str(raw if isinstance(raw, str) else
                  (args.get("cmd") or args.get("command") or "")).strip()
        if not cmd:
            return None, "缺少 argv（命令与参数数组）"
        from_string = True
        try:
            argv = [t.strip('"').strip("'") for t in shlex.split(cmd, posix=False)]
        except ValueError as e:
            return None, "命令无法解析：%s" % e
    if not argv:
        return None, "命令为空"
    if from_string:
        for tok in argv:
            if any(m in tok for m in CMD_META_CHARS):
                return None, "不支持管道 / 重定向 / 命令串联：%s" % tok
    exe = _exe_name(argv[0])
    if exe in CMD_DENY_EXES:
        return None, "该命令被禁止执行：%s" % exe
    if exe not in CMD_ALLOW_EXES:
        return None, "只允许执行：%s" % "、".join(sorted(CMD_ALLOW_EXES))
    rest = argv[1:]
    problem = _cmd_inline_error(exe, rest) or _cmd_script_error(exe, rest, root)
    if problem:
        return None, problem
    return argv, ""


def _resolve_argv(argv):
    """strict 启动前补全可执行文件路径（Windows 的 .cmd 需要）。

    `Popen(shell=False)` 在 Windows 上交给 CreateProcess，而它只按
    `.exe` 查找；npm / npx 实际是 `npm.cmd` / `npx.cmd`，裸名字会
    FileNotFoundError。`shutil.which` 会按 PATHEXT 找到 `.CMD`，补成
    完整路径后就能启动。显示文本仍用原始 argv，不暴露完整路径。
    """
    if not argv:
        return argv
    exe = str(argv[0])
    if os.path.isabs(exe):
        return list(argv)
    found = shutil.which(exe)
    if not found:
        return list(argv)
    return [found] + [str(a) for a in argv[1:]]


# ---- Shell 自由度：三档（默认 strict，行为与从前完全一致）----


SHELL_MODES = ("strict", "standard", "trusted")


SHELL_DEFAULT = "strict"


def normalize_shell_mode(raw):
    m = str(raw or "").strip().lower()
    return m if m in SHELL_MODES else SHELL_DEFAULT


def _pipe_plan(cmd, root):
    """拆 `|` 与每段末尾的重定向，逐段过白名单。"""
    from .sandbox import (SandboxError, resolve_path)
    segments = []
    for seg in cmd.split("|"):
        seg = seg.strip()
        if not seg:
            return None, "管道两侧不能为空"
        m = re.search(r"(>>?)\s*(\"[^\"]+\"|'[^']+'|\S+)\s*$", seg)
        redirect, target = "", ""
        if m:
            redirect = m.group(1)
            raw_target = m.group(2).strip().strip('"').strip("'")
            seg = seg[:m.start()].strip()
            try:
                target = resolve_path(root, raw_target)
            except SandboxError as e:
                return None, "重定向目标必须在沙箱内：%s" % e
        if ">" in seg or "|" in seg:
            return None, "重定向只能出现在每一段的末尾：%s" % seg[:80]
        if not seg:
            return None, "重定向前面缺少命令"
        if re.search(r"[\"'][^\"']*[|>][^\"']*[\"']", seg):
            return None, "引号内不允许管道或重定向：%s" % seg[:80]
        argv, err = _cmd_argv({"argv": seg}, root)
        if err:
            return None, "第 %d 段：%s" % (len(segments) + 1, err)
        segments.append({"argv": argv, "redirect": redirect, "target": target})
    return segments, ""


def _shell_plan(cmd, root, shell_mode):
    """把一条含管道 / 重定向的命令拆成可逐段校验的计划。

    standard：只允许 `|` 和沙箱内重定向 `>` `>>`；
    trusted ：额外允许 `;` `&&` `||` 串联（仍逐条校验）。
    引号内出现管道 / 重定向一律拒绝 —— shlex 与 cmd.exe 的解析规则不同，
    这种歧义正是命令注入的入口。
    """
    for tok in ("`", "$(", "<", "%COMSPEC%"):
        if tok in cmd:
            return None, "该模式不允许 %r：%s" % (tok, cmd[:120])
    # 单个 & 是后台执行，任何档位都不放行（&& 是串联，另行拆分）
    if "&" in cmd.replace("&&", ""):
        return None, "不支持后台执行符 &：%s" % cmd[:120]
    if shell_mode != "trusted" and ";" in cmd:
        return None, "该模式不允许 ; 串联命令：%s" % cmd[:120]
    if root is None:
        return None, "缺少沙箱根目录"

    parts = (re.split(r"\s*(?:&&|\|\||;)\s*", cmd)
             if shell_mode == "trusted" else [cmd])
    segments = []
    for part in parts:
        if not part.strip():
            return None, "命令串联处为空"
        sub, err = _pipe_plan(part.strip(), root)
        if err:
            return None, err
        segments.extend(sub)
    return {"shell": True, "cmd": cmd, "segments": segments, "argv": None}, ""


def _cmd_plan(args, root=None, shell_mode="strict"):
    """把工具参数收敛成可执行计划（argv 直跑 / 交给 shell 解释）。

    strict 走原来的 `_cmd_argv`，行为与从前完全一致；另外两档才允许管道。
    无论哪一档，每一段都要单独过白名单与内联执行检查。
    """
    shell_mode = normalize_shell_mode(shell_mode)
    args = args if isinstance(args, dict) else {}
    raw = args.get("argv")
    if isinstance(raw, list) and raw:
        argv, err = _cmd_argv(args, root)
        return ({"shell": False, "argv": argv, "cmd": "", "segments": []}
                if not err else None), err
    cmd = str(raw if isinstance(raw, str) else
              (args.get("cmd") or args.get("command") or "")).strip()
    if not cmd:
        return None, "缺少 argv（命令与参数数组）"
    if shell_mode == "strict":
        argv, err = _cmd_argv(args, root)
        return ({"shell": False, "argv": argv, "cmd": "", "segments": []}
                if not err else None), err
    return _shell_plan(cmd, root, shell_mode)


def _cmd_plan_unrestricted(args):
    """完全放权：命令原样执行，不做白名单 / 内联 / 管道校验。

    只保证「有东西可跑」；工作目录仍由 `_t_run_command` 锁在沙箱内，
    所以放权的是「能跑什么」，不是「能跑到哪」。
    """
    args = args if isinstance(args, dict) else {}
    raw = args.get("argv")
    if isinstance(raw, list) and raw:
        argv = [str(x) for x in raw if str(x) != ""]
        if not argv:
            return None, "命令为空"
        return {"shell": False, "argv": argv, "cmd": "", "segments": []}, ""
    cmd = str(raw if isinstance(raw, str) else
              (args.get("cmd") or args.get("command") or "")).strip()
    if not cmd:
        return None, "缺少 argv（命令与参数数组）"
    return {"shell": True, "cmd": cmd, "segments": [], "argv": None}, ""


def _cmd_kind(argv):
    """命令风险分级：readonly（只读）/ normal（跑代码）/ high（安装、联网、改历史）。"""
    exe = _exe_name(argv[0])
    rest = [str(a).lower() for a in argv[1:]]
    if exe == "git":
        sub = rest[0] if rest else ""
        if sub in CMD_GIT_READONLY:
            return "readonly"
        if sub in CMD_GIT_MEDIUM:
            return "normal"
        return "high"
    if exe in ("npm", "npx"):
        sub = rest[0] if rest else ""
        if sub in ("--version", "-v", "ls", "list", "view"):
            return "readonly"
        if exe == "npx" or sub in CMD_NPM_HIGH:
            return "high"
        return "normal"
    if exe in ("pip", "pip3"):
        sub = rest[0] if rest else ""
        if sub in ("install", "uninstall", "download"):
            return "high"
        return "readonly" if sub in ("list", "show", "freeze", "--version", "-v") else "normal"
    if exe in ("python", "python3", "py", "node", "nodejs"):
        if len(rest) == 1 and rest[0] in ("--version", "-v", "-version"):
            return "readonly"
        if "-m" in rest:
            i = rest.index("-m")
            mod = rest[i + 1] if i + 1 < len(rest) else ""
            if mod in ("pip", "ensurepip"):
                return "high"
            if mod == "pytest" and "--collect-only" in rest:
                return "readonly"
        return "normal"
    if exe == "pytest":
        return "readonly" if ("--collect-only" in rest or "--version" in rest) else "normal"
    if exe == "ruff":
        return "readonly" if rest and rest[0] in ("check", "--version") else "normal"
    if exe in ("pnpm", "yarn", "bun"):
        sub = rest[0] if rest else ""
        if sub in ("install", "i", "add", "update", "upgrade", "publish", "link"):
            return "high"
        return "normal"
    if exe == "go":
        sub = rest[0] if rest else ""
        if sub in ("get", "install"):
            return "high"
        return "normal"
    if exe == "cargo":
        sub = rest[0] if rest else ""
        if sub in ("add", "install", "publish", "yank"):
            return "high"
        return "normal"
    if exe in ("mvn", "mvnw", "gradle", "gradlew"):
        sub = rest[0] if rest else ""
        return "high" if sub in ("deploy", "publish") else "normal"
    if exe == "dotnet":
        sub = rest[0] if rest else ""
        if sub in ("add", "restore", "nuget", "tool"):
            return "high"
        return "normal"
    return "normal"


def _cmd_env():
    """透传给子进程的环境变量：名字里带 key/token/secret… 的一律不传。"""
    env = {k: v for k, v in os.environ.items() if not CMD_ENV_DROP.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("NODE_OPTIONS", None)
    return env


def _clean_output(raw):
    if not raw:
        return ""
    if isinstance(raw, bytes):
        for enc in ("utf-8", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)     # 去 ANSI 色码
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > CMD_MAX_OUTPUT:
        head, tail = 12000, 8000
        cut = len(text) - head - tail
        text = (text[:head] + "\n…（输出过长，中间省略 %d 字）…\n" % cut + text[-tail:])
    return text


# 退出码 0 时也出现的「假报错」：和真错误长得一样，会把模型带偏。
# 只在成功时过滤 —— 失败时保留全部输出，别把真错误一起藏了。


def _strip_cmd_noise(text, returncode):
    from .base import _CMD_NOISE_PREFIXES
    if returncode != 0 or not text:
        return text
    lines = text.splitlines()
    kept = [ln for ln in lines if not ln.strip().lower().startswith(_CMD_NOISE_PREFIXES)]
    return text if len(kept) == len(lines) else "\n".join(kept).strip()


def _kill_tree(pid):
    """超时后连同子进程一起杀掉（否则 pytest / node 的子进程会残留）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=winproc.no_window())
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def _cmd_secret_hit(root, args):
    """命令参数里直接点名了密钥文件 → 返回相对路径（否则空串）。

    只做字符串层面的兜底：`cat .env` / `type .env` 这类能被抓住；
    脚本内部再打开文件抓不到 —— 那要靠外联审计与审批兜底。
    """
    from .approval import is_secret_path
    from .sandbox import _rel
    args = args if isinstance(args, dict) else {}
    raw = args.get("argv")
    if isinstance(raw, list) and raw:
        toks = [str(x) for x in raw]
    else:
        cmd = str(raw if isinstance(raw, str) else
                  (args.get("cmd") or args.get("command") or ""))
        try:
            toks = shlex.split(cmd, posix=False)
        except ValueError:
            toks = cmd.split()
    for tok in toks:
        t = tok.strip().strip("\"'")
        if not is_secret_path(t):
            continue
        if os.path.isabs(t):
            return _rel(root, t)
        rel = t.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        return rel
    return ""


def _t_run_command(root, args, stop_event=None, shell_mode="strict",
                   unrestricted=False):
    """在沙箱内执行一条前台命令，返回（退出码 + 输出, None）。

    stop_event 被 set 时立刻结束命令（含子进程），不用等到超时 ——
    pytest / 装依赖这类命令能跑几十秒，用户点「停止」不该一直等它跑完。

    shell_mode 决定能不能用管道：strict 走 argv 直跑（默认，与从前一致）；
    standard / trusted 才交给 shell 解释，且每一段都已单独校验过。
    unrestricted=True（完全放权）时不再做任何校验，命令原样执行。
    """
    from .approval import (_secret_allowed, _secret_deny_text)
    from .sandbox import (SandboxError, _rel, clean_cwd, resolve_path)
    plan, err = (_cmd_plan_unrestricted(args) if unrestricted
                 else _cmd_plan(args, root, shell_mode))
    if err:
        return "❌ " + err, None
    display = plan["cmd"] or " ".join(plan["argv"])
    # 秘密文件护栏：命令直接点名 .env / 私钥这类文件时不执行
    _sec = _cmd_secret_hit(root, args)
    if _sec and not _secret_allowed(root, [_sec]):
        return _secret_deny_text(_sec), None
    try:
        workdir = resolve_path(root, clean_cwd((args or {}).get("cwd")))
    except SandboxError as e:
        return "❌ " + str(e), None
    if not os.path.isdir(workdir):
        return "❌ 工作目录不存在：" + _rel(root, workdir), None
    try:
        timeout = int((args or {}).get("timeout") or CMD_DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = CMD_DEFAULT_TIMEOUT
    if timeout < 0:
        timeout = 0                     # -1 = 不限：等到它自己结束，或你点停止
    elif timeout == 0:
        timeout = CMD_DEFAULT_TIMEOUT
    else:
        timeout = min(timeout, CMD_MAX_TIMEOUT)
    t0 = time.time()
    try:
        if plan["shell"]:
            proc = subprocess.Popen(display, cwd=workdir, env=_cmd_env(), shell=True,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=winproc.no_window())
        else:
            # strict：Popen 不会把 npm 解析成 npm.cmd，先按 PATH/PATHEXT 补全
            proc = subprocess.Popen(_resolve_argv(plan["argv"]), cwd=workdir,
                                    env=_cmd_env(), shell=False,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=winproc.no_window())
    except FileNotFoundError:
        return "❌ 找不到命令：%s（可能没装，或不在 PATH 里）" % (
            plan["argv"][0] if plan["argv"] else display), None
    except OSError as e:
        return "❌ 无法启动命令：%s" % e, None
    # 外联审计（只观察，不拦截）：记录这条命令及其子进程连过哪些外部地址。
    # 采集失败一律静默，绝不能因为审计把命令本身搞挂。
    _audit = None
    try:
        if bool((load_config() or {}).get("agent_net_audit", True)):
            from app import netwatch
            _audit = netwatch.Watcher(proc.pid).start()
    except Exception:
        _audit = None

    def _audit_tail():
        if _audit is None:
            return ""
        try:
            from app import netwatch
            note = netwatch.summary(_audit.stop())
        except Exception:
            return ""
        return ("\n\n【外联审计】" + note) if note else ""

    try:
        # 边等边看停止信号：communicate 超时不会丢输出，可以放心地 0.2 秒轮一次
        deadline = (time.time() + timeout) if timeout else None   # None = 不限超时
        while True:
            if stop_event is not None and stop_event.is_set():
                _kill_tree(proc.pid)
                try:
                    out, _ = proc.communicate(timeout=5)
                except Exception:
                    out = b""
                body = _clean_output(out)
                return ("⏹ 已按你的要求终止命令。\n命令：%s\n%s%s"
                        % (display, body, _audit_tail()), None)
            if deadline is None:
                wait = 0.2
            else:
                remain = deadline - time.time()
                if remain <= 0:
                    raise subprocess.TimeoutExpired(display, timeout)
                wait = min(0.2, remain)
            try:
                out, _ = proc.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = b""
        body = _clean_output(out)
        return ("❌ 命令超时（%ds）已被终止。\n命令：%s\n%s%s"
                % (timeout, display, body, _audit_tail()), None)
    used = time.time() - t0
    body = _strip_cmd_noise(_clean_output(out), proc.returncode)
    verdict = "成功" if proc.returncode == 0 else "失败"
    head = ("命令：%s\n工作目录：%s\n结论：%s（退出码：%d，用时 %.1fs）"
            % (display, _rel(root, workdir), verdict, proc.returncode, used))
    return (head + ("\n输出：\n" + body if body else "\n输出：（空）")
            + _audit_tail()), None


# 持久 shell：每个会话一个独立终端（sid:agent），与 IDE 面板的终端互不干扰。
# 模型读写的是「绝对偏移」，所以只要记住上次读到哪里，就能增量取输出。


SHELL_WAIT_DEFAULT = 8


SHELL_WAIT_MAX = 60


def _shell_idle(text):
    """输出末尾是不是已经回到 PowerShell 提示符（说明命令跑完了）。

    不能用「安静 1 秒」判断结束：`Start-Sleep 2; echo x` 会先回显命令、
    再静默 2 秒才输出结果，按安静判断会提前返回只有回显的半截结果。
    提示符后面通常还跟着 ANSI 光标控制码，所以先去掉再匹配。
    """
    tail = (text or "")[-1200:]
    tail = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", tail)[-400:]
    return bool(re.search(r"(?m)PS [A-Za-z]:\\[^>\r\n]*>\s*$", tail))


def _t_shell(root, args, caps=None, stop_event=None):
    """持久 PowerShell：run / read / interrupt / close。

    与 run_command 的区别：同一个 shell 进程，cd / 环境变量 / 后台进程都会保留；
    可以发管道、跑交互式命令、起 dev server 后再接着操作。
    读输出用绝对偏移，避免把上一轮的内容重复喂给模型。
    """
    from .base import (_SHELL_LOCK, _SHELL_OFF)
    from app import terminal
    sid = str((caps or {}).get("sid") or "").strip()
    if not sid:
        return "❌ shell 需要会话 id（caps.sid）", None
    action = str((args or {}).get("action") or "run").strip().lower()
    key = sid + ":agent"
    if action == "close":
        with _SHELL_LOCK:
            _SHELL_OFF.pop(key, None)
        terminal.drop_by_sid(key)
        return "已关闭 shell", None
    if action not in ("run", "read", "interrupt"):
        return "❌ action 只能是 run / read / interrupt / close", None
    try:
        t = terminal.start(key, root, env=_cmd_env(), shell=terminal.SHELL_AGENT)
    except terminal.TerminalError as e:
        return "❌ %s" % e, None
    with _SHELL_LOCK:
        first = key not in _SHELL_OFF
    if first and action == "run":
        # 首次创建：等提示符出现再写命令，否则命令可能发在 PowerShell 就绪之前
        deadline = time.time() + 6
        while time.time() < deadline and not t.snapshot(0)["text"]:
            time.sleep(0.15)
    with _SHELL_LOCK:
        off = _SHELL_OFF.get(key)
        _now_off = t.snapshot(0)["off"]
        if off is None or off > _now_off:
            # 首次使用，或旧进程被回收后偏移已经失效：从当前末尾开始读，
            # 别把启动横幅当成命令输出，也别让过期的偏移永远读不到新内容
            off = _now_off
            _SHELL_OFF[key] = off
    if action == "interrupt":
        return ("已发送 Ctrl+C", None) if t.interrupt() else ("❌ 发送 Ctrl+C 失败", None)
    if action == "read":
        snap = t.snapshot(off)
        with _SHELL_LOCK:
            _SHELL_OFF[key] = snap["off"]
        return (_clean_output(snap["text"]) or "（没有新输出）"), None
    command = str((args or {}).get("command") or "").strip()
    if not command:
        return "❌ run 需要 command", None
    if not t.write(command + "\r"):
        return "❌ shell 进程已退出，请重新执行（会自动重建）", None
    try:
        wait = int((args or {}).get("wait") or SHELL_WAIT_DEFAULT)
    except (TypeError, ValueError):
        wait = SHELL_WAIT_DEFAULT
    wait = max(1, min(wait, SHELL_WAIT_MAX))
    deadline = time.time() + wait
    got = ""
    snap = t.snapshot(off)
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            break
        snap = t.snapshot(off)
        if snap["text"]:
            got += snap["text"]
            off = snap["off"]
        # 回到提示符 = 命令跑完了；否则继续等到 wait 上限（慢命令先回显后输出）
        if got and _shell_idle(got):
            break
        time.sleep(0.25)
    with _SHELL_LOCK:
        _SHELL_OFF[key] = off
    body = _clean_output(got)
    if not body:
        return ("（命令已发送，%d 秒内没有输出；可能没被终端接收。"
                "请重试一次，或用 action=read 继续取输出）" % wait), None
    if time.time() >= deadline and got:
        # 等到 wait 上限才停：命令可能还在跑，提醒模型可以继续取输出
        body += "\n\n（命令可能仍在运行：用 read 继续取输出，interrupt 中断）"
    return body, None


# 命令相关函数依赖 resolve_path / load_rules，只能放在 _HANDLERS 之后注册


def _script_stamp(root, args):
    """run_command 里引用的脚本文件（相对路径 + .py/.js…）的 mtime+size 指纹。"""
    a = args if isinstance(args, dict) else {}
    argv = a.get("argv")
    if not isinstance(argv, list):
        return ""
    out = []
    for tok in argv:
        t = str(tok or "")
        if not t or t.startswith("-") or " " in t:
            continue
        if os.path.splitext(t)[1].lower() not in (".py", ".js", ".mjs", ".ps1", ".sh"):
            continue
        p = t if os.path.isabs(t) else os.path.join(root, t)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append("%s:%d:%d" % (t, st.st_mtime_ns, st.st_size))
        if len(out) >= 3:
            break
    return ",".join(out)


def call_signature(name, args, root=None):
    """工具调用的稳定指纹：同名 + 同参数（键排序），用于识别「原地打转」。

    root 非空时，run_command 会把 argv 引用的脚本文件（mtime, size）也算进指纹：
    改了脚本再跑算一次新调用，脚本没变还反复跑才算重复。
    """
    try:
        blob = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        blob = str(args)
    sig = "%s|%s" % (name or "", blob)
    if root and name == "run_command":
        stamp = _script_stamp(root, args)
        if stamp:
            sig += "|" + stamp
    return sig
