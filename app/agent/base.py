# -*- coding: utf-8 -*-

import json
import os
import re
import threading
from app import langs
from app.config import load_config
from app.mem_tool import DSML_CLOSE, DSML_OPEN, MemoryExtractor



"""会话沙箱内的「文件 Agent」——让模型在主对话里自主调用文件工具

- 协议：模型每轮回复末尾可追加一个 ```tool 代码块（JSON），说明要调用哪个工具。
  后端摘掉这个块，执行工具，把结果追加进对话，再让模型继续，直到它给出最终回答。
- 本模块负责：
  1. build_agent_prompt(s)  生成注入系统提示的工具说明（含沙箱路径与目录树）
  2. ToolExtractor          从流式文本里把 ```tool 块摘出来（用户看不到）
  3. resolve_path()         沙箱路径解析 + 越界 / 敏感目录拦截
  4. run_tool()             执行工具；写操作额外返回可撤回的变更记录
  5. new_asks / wait_answers  向用户提问（可一次多个）并在原地等待回答
  6. undo(token)            撤回一次写操作（恢复文件快照）
  7. pick_directory()       弹出系统文件夹选择框
- 安全：命令执行仅限白名单可执行文件（常见语言工具链 / 构建器 / git / 包管理器），
  禁止管道与重定向、cwd 锁沙箱、环境变量清洗；所有文件路径限制在沙箱内；
  data/ 目录永久禁止访问。持久 shell（shell 工具）等于任意命令执行，
  不复用白名单，单独走高危审批。**这不是 OS 级沙箱**，只是有边界的便利。
"""


FENCE = "```tool"


MAX_ROUNDS = 0              # 单轮对话里最多循环多少次「调工具 → 再问模型」；0 = 不限


MAX_READ_FILE_BYTES = 1024 * 1024  # read_file 单次直接读取上限（放宽，别为看大文件去写脚本）


MAX_READ_FILES = 16          # read_files 一次最多读几个文件


MAX_LIST_ENTRIES = 200


MAX_SEARCH_HITS = 60        # 搜索结果最多列多少行


MAX_SEARCH_PER_FILE = 5     # 同一个文件最多列几处命中


MAX_SEARCH_CONTEXT = 5      # 命中行前后各取几行上下文


MAX_SEARCH_FILES = 80       # count_only 时最多列多少个文件


MAX_CALLS_PER_ROUND = 10    # 模型单轮最多一次给出多少个工具调用


MAX_CHECK_BYTES = 512 * 1024   # 写后自检的单文件上限


REPEAT_LIMIT = 5                            # 同一工具 + 同一参数连续调用几次就拦截（对齐 dsh 的 3/5/8 提醒档）
# 死循环需要两条互补的判据：
#   连续相同 → repeat_guard（读 A、读 B、再读 A 这种正常来回不误杀）
#   窗口内重复 → window_hits（build→render→gap→build 这种交错循环，连续判据看不见）


LOOP_WINDOW = 24                            # 回溯最近多少次工具调用


LOOP_WINDOW_LIMIT = 4                       # 一般工具：窗口内同名同参出现 4 次


LOOP_WINDOW_LIMIT_CMD = 3                   # 命令：脚本没改还反复跑，第 3 次就拦
# 只读 / 状态类工具不进窗口：验收查状态、读步骤这类重复属于正常操作，
# 而且它们本身不产生副作用；真正要拦的是「反复重建 / 反复渲染」。


LOOP_EXEMPT = ("acceptance", "list_steps", "read_tool_result", "sleep", "ask_user",
               "image_compare", "image_analyze")

# 可在「设置 → Agent」里调整的运行时上限：键 → 允许的最小值。
# 默认值仍是上面那些常量，所以从没配置过时行为与从前完全一致。
# 值填 0 = 「不限」：上限由用户自己取舍，代码不再替他定天花板（负数视为填错，回落默认）。


LIMIT_MINIMUMS = {
    "max_rounds": 1,
    "repeat_limit": 1,
    "cmd_timeout": 1,
}

# ---- 命令执行（run_command）----


VISION_NATIVE_MAX = 8                 # 原生附图：一轮最多附几张（保留到本轮结束）
# 命令跑完后自动抽查新生成的图片：空白 / 纯色通常是「渲染没加载场景」这类失败


BLANK_STD_MIN = 2.0                   # 灰度标准差低于它视为几乎全空白


BLANK_RANGE_MIN = 8.0                 # 灰度极差低于它视为纯色


BLANK_SCAN_MAX = 3000                 # 单次最多看多少个图片文件（防止大仓库拖慢）


BUILD_LOCK = threading.RLock()


def ToolExtractor():
    """复用记忆工具的流式摘除器：认 ```tool 围栏，也认 DeepSeek 原生工具调用标记。

    模型偶尔不用我们的围栏，而是吐它自己的原生标记（DSML 标记）。不认它就会把整块标记
    直接显示给用户、工具不执行、本轮静默结束 —— 表现为「输出中断」。
    """
    return MemoryExtractor(FENCE, openers=[(FENCE, "```"), (DSML_OPEN, DSML_CLOSE)])


def _ndjson_objects(text):
    """按行解析成串的 JSON 对象（DeepSeek 原生格式就是一行一个）"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _first_arg(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _glob_to_regex(pat):
    """把 glob 转成匹配相对路径的正则；支持 ** 跨目录（* 不跨 /）。"""
    pat = (pat or "").replace("\\", "/").strip()
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")                     # **/ 匹配任意层（含 0 层）
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


SYMBOL_EXTS = set(langs.SOURCE_EXTS)


SYMBOL_MAX_FILES = 800


SYMBOL_MAX_BYTES = 512 * 1024


SYMBOL_REF_LIMIT = 40          # 引用最多列多少条，避免刷屏


SYMBOL_PER_FILE_REFS = 3       # 同一个文件里最多列几条引用


CHECK_MAX_FILES = 200


def _symbol_def_re(name):
    esc = re.escape(name)
    pats = [
        r"^\s*(?:async\s+)?def\s+%s\s*\(" % esc,                      # python
        r"^\s*class\s+%s\b" % esc,                                    # py / js / ts / java
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+%s\s*\(" % esc,
        r"^\s*(?:export\s+)?(?:const|let|var)\s+%s\s*=" % esc,        # 变量 / 箭头函数
        r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+%s\s*[<(]" % esc,          # rust
        r"^\s*func\s+(?:\([^)]*\)\s*)?%s\s*\(" % esc,                 # go
        r"^\s*(?:typedef\s+)?(?:struct|enum|union|interface|trait|type)\s+%s\b" % esc,
        r"^\s*#define\s+%s\b" % esc,
        # java / c / c++ / c# 的方法定义：修饰符 + 返回类型 + 名字(
        # 排除 return / if / for 这类「调用行」，否则会把普通调用误判成定义
        r"^\s*(?!return\b|if\b|while\b|for\b|switch\b|catch\b|throw\b|await\b"
        r"|new\b|delete\b|case\b|do\b|try\b|else\b)"
        r"(?:(?:public|private|protected|internal|static|final|virtual|override"
        r"|async|synchronized|const|extern|inline|unsigned|signed)\s+)*"
        r"[A-Za-z_][\w:<>\[\],.?*&]*\s+%s\s*\(" % esc,
    ]
    return re.compile("|".join(pats), re.M)


CODE_CONTEXT_CHARS = 9000


CODE_CONTEXT_REFS = 12


CODE_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$.:]{2,80}$")


VERIFY_TIMEOUT = 120          # 单次验证最多跑多久


VERIFY_MAX_TESTS = 6          # 最多同时验证几个测试文件


def _t_split_commit(root, args):
    """按逻辑分组提交：每组一个 commit，组内先跑相关测试。

    这是对方没有的能力：一个任务改了多个模块时，历史里应该是几条语义清晰的提交，
    而不是一条「改了很多」。文件清单由代码校验，模型只负责写「为什么」。
    """
    from .tools_git import verify_before_commit
    from app import git as gitlib
    groups = args.get("groups")
    if not isinstance(groups, list) or not groups:
        return "请提供 groups：[{message, paths}]", None
    seen, plan = set(), []
    for i, g in enumerate(groups):
        g = g if isinstance(g, dict) else {}
        msg = str(g.get("message") or "").strip()
        paths = g.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not msg:
            return "第 %d 组缺少 message" % (i + 1), None
        if not isinstance(paths, list) or not paths:
            return "第 %d 组缺少 paths（要提交哪些文件）" % (i + 1), None
        clean = []
        for p in paths:
            rel = str(p).replace("\\", "/").strip().lstrip("./")
            if not rel:
                continue
            if rel in seen:
                return "文件 %s 出现在多个分组里，请先决定它属于哪一组" % rel, None
            seen.add(rel)
            clean.append(rel)
        if not clean:
            return "第 %d 组没有有效路径" % (i + 1), None
        plan.append((msg, clean))

    verify_mode = str(load_config().get("git_verify_before_commit")
                      or "auto").strip().lower()
    done, notes = [], []
    for i, (msg, paths) in enumerate(plan, 1):
        if verify_mode != "off":
            ok, note = verify_before_commit(root, paths)
            if not ok:
                return ("❌ 第 %d 组验证没通过，已停止（前 %d 组已提交）。\n%s\n\n%s"
                        % (i, len(done), note, "\n".join(notes))), None
            if note.startswith("已通过"):
                msg += "\n\nVerified-by: " + note.replace("已通过：", "")
        try:
            r = gitlib.commit(root, msg, rels=paths)
        except gitlib.GitError as e:
            return ("❌ 第 %d 组提交失败：%s（前 %d 组已提交）\n%s"
                    % (i, e, len(done), "\n".join(notes))), None
        done.append(r.get("hash") or "")
        notes.append("  %d. %s  %s" % (i, r.get("hash") or "",
                                       msg.splitlines()[0]))
    return "已分 %d 组提交：\n%s" % (len(done), "\n".join(notes)), None


def post_write_check(path):
    """写后语法自检；跨语言逻辑在 app.langs.syntax_check（缺工具就静默跳过）。"""
    return langs.syntax_check(path)


MAX_TURNS = 5               # 只保留最近几个会话的检查点，避免内存堆积


def _limit_or_default(raw, default):
    """取一项可调上限：None / 非数字 → 默认值；0 原样返回（调用方按「不限」处理）。"""
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def normalize_auto_continue(raw):
    """自动续跑次数：0 = 关闭，1–10 = 次数，-1 = 无上限（非法值回落到 3）。

    无上限只是「不按次数刹车」：工具轮次仍受 max_rounds 限制，
    连续空转另有保护（见 chat.py 的 auto_idle）。
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 3
    if n < 0:
        return -1
    return max(0, min(10, n))


_CMD_NOISE_PREFIXES = (
    "exception in module register():",          # Blender / bpy 每次 import 都打印
    "warning: ignoring invalid distribution",   # pip 的站点包残留警告
)


_SHELL_OFF = {}


_SHELL_LOCK = threading.Lock()


SLEEP_MAX = 30


FETCH_URL_CHARS = 12000


FETCH_URL_MAX_CHARS = 40000


_PLAN_REVIEW_ACTIONS = ("approve", "comment", "edit")


def repeat_guard(state, sig, limit=None):
    """state 是可变的 {"sig": str, "n": int}。返回 True 表示这次应当拦截。

    只统计**连续**相同：中间夹一次不同的调用就重新计数，
    避免把「读文件 A、读文件 B、再读 A」这种正常来回误判成死循环。
    limit 不传时用模块常量 REPEAT_LIMIT；0 = 不限（设置里选了「不限」，永远不拦）。
    """
    if limit is None:
        limit = REPEAT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = REPEAT_LIMIT
    if limit <= 0:
        return False
    if sig == state.get("sig"):
        state["n"] = int(state.get("n") or 0) + 1
    else:
        state["sig"] = sig
        state["n"] = 1
    return state["n"] >= limit


def window_hits(state, sig, window=None, limit=None):
    """滑动窗口重复：最近 window 次调用里同一指纹出现 >= limit 次 → True。

    和 repeat_guard 互补：build → render → gap → build 这种交错循环，连续判据
    永远看不见（中间夹一次别的就清零了），窗口判据能看出来。
    被拦的调用也记进历史，避免「拦住一次 → 计数清空 → 继续转」。
    """
    w = max(2, int(window or LOOP_WINDOW))
    lim = max(2, int(limit or LOOP_WINDOW_LIMIT))
    hist = state.setdefault("hist", [])
    hist.append(sig)
    if len(hist) > w:
        del hist[:-w]
    return hist.count(sig) >= lim


def reset_loop_window(state):
    """一次成功的写入 = 有进展：清空窗口，避免「改脚本 → 再跑」被误判成死循环。"""
    if isinstance(state, dict):
        state["hist"] = []


def result_ok(name, text):
    """工具是否成功：除了 ❌ 前缀，run_command 还要看真实退出码。

    以前只认「文本不以 ❌ 开头」，于是退出码 1 的命令在时间线里也是 ok:true，
    模型和用户都会误判成「这一步过了」。
    """
    t = str(text or "").lstrip()
    if t.startswith("❌"):
        return False
    if name == "run_command":
        m = re.search(r"退出码：(-?\d+)", t)
        if m:
            try:
                return int(m.group(1)) == 0
            except ValueError:
                return True
    return True


_FAIL_KEYS = ("path", "target", "command", "argv", "pattern", "query", "url",
              "batch", "token", "id", "cwd")


def failure_signature(name, args, err=""):
    """失败归因指纹：忽略 viewport / screenshot / timeout / start / limit 这类表面参数。"""
    a = args if isinstance(args, dict) else {}
    picked = {k: a[k] for k in _FAIL_KEYS if k in a}
    m = re.search(r"(SANDBOX_DENIED|NOT_FOUND|TIMEOUT|INVALID_ARG|PERMISSION|TOOL_ERROR)",
                  str(err or ""))
    return "%s|%s|%s" % (name or "",
                         json.dumps(picked, sort_keys=True, ensure_ascii=False, default=str),
                         m.group(1) if m else "")


def failure_streak(state, key, limit=2):
    """state 是可变的 {"key": str, "n": int}；返回同一目标连续失败的次数。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 2
    if limit <= 0:
        return 0
    if key and key == state.get("key"):
        state["n"] = int(state.get("n") or 0) + 1
    else:
        state["key"] = key
        state["n"] = 1
    return int(state.get("n") or 0)


# ============================================================
# 3.5 执行计划（Plan / TODO）
# ============================================================


def _normalize_options(raw):
    """选项统一成 {label, desc, recommended}；兼容旧的纯字符串写法。"""
    out = []
    for o in (raw or [])[:6]:
        if isinstance(o, dict):
            label = str(o.get("label") or o.get("text") or "").strip()
            desc = str(o.get("desc") or o.get("detail") or "").strip()
            rec = bool(o.get("recommended"))
        else:
            label = str(o).strip()
            desc = ""
            rec = False
        if not label:
            continue
        out.append({"label": label[:200], "desc": desc[:400], "recommended": rec})
    return out


_PATH_EXT = (r"py|js|ts|tsx|jsx|json|md|txt|html|css|scss|less|yaml|yml|toml|"
             r"ini|cfg|conf|log|csv|xml|pdf|docx|xlsx|pptx|sh|bat|ps1|java|kt|"
             r"c|cc|cpp|h|hpp|go|rs|sql|env")


_PATH_RE = re.compile(
    r'(?:"([^"\n]+)"|\'([^\'\n]+)\'|'
    r'((?:[A-Za-z]:[\\/]|\.{0,2}/)?[\w\u4e00-\u9fff@%+.,-]+'
    r'(?:[\\/][\w\u4e00-\u9fff@%+.,-]+)*\.(?:' + _PATH_EXT + r')))', re.I)


def intent_hint_prompt(hint):
    if not hint:
        return ""
    try:
        args = json.dumps(hint.get("args") or {}, ensure_ascii=False)
    except Exception:
        args = "{}"
    return "\n".join([
        "## 本轮意图路由（系统预判，仅供参考）",
        "用户本轮很可能需要调用 `%s`，建议参数：%s。" % (hint.get("tool"), args),
        "如果这与用户原意一致，请在本轮直接调用；不一致则以用户原意为准。",
        "缺少必需参数时，先用 ask_user 问清楚；不要猜路径、编链接或假设文件名。",
    ])


_PICK_LOCK = threading.Lock()


_TREE_CACHE = {}


_TREE_LOCK = threading.Lock()


def rename_entry(sandbox, rel, new_name):
    """在同一目录内重命名"""
    from .sandbox import (SandboxError, _norm_rel, resolve_path)
    new_name = (new_name or "").strip()
    if not new_name or "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        raise SandboxError("新名称不合法")
    src = resolve_path(sandbox, rel)
    parent_rel = os.path.dirname(_norm_rel(rel))
    dst = resolve_path(sandbox, os.path.join(parent_rel, new_name) if parent_rel else new_name)
    if os.path.exists(dst):
        raise SandboxError("目标已存在：" + new_name)
    os.replace(src, dst)
    return True
