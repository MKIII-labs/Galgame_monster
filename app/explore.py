# -*- coding: utf-8 -*-
"""并行探索子代理（可信侦察队）。

主代理调用 explore 工具，派出 1–5 个侦察兵：
- 每个子代理**独立上下文**，只拿「原始诉求 + 它自己的问题 + 已知线索」；
- 默认可读；开启写入后可以改沙箱内文件，但每次写都走主循环的审批；
- 结论必须带「文件:行号」，系统会**重新读取核对**，引用不存在就重试一次；
- 支持可机检的验收条件（cites>=N / answers>=N / contains:...）；
- 子会话落盘，进程重启后仍可 explore_followup 冷恢复。

这样主线程既拿到多路交叉验证的结论，又不会被几十个文件的原文撑爆。
"""
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait

from app import agent
from app.config import effective_think_mode, thinking_payload
from app.store import DATA_DIR

MAX_QUESTIONS = 12
MAX_DEPTH = 5                  # 子代理最多再往下派 5 层（根 depth=0）
MAX_TOTAL = 100                # 一棵子代理树最多派几个（0 = 不限）
MAX_SUB_ROUNDS = 10
SUB_MAX_TOKENS = 2000
SUB_TIMEOUT = 60.0             # 单个侦察兵最长多久
CRITIC_TIMEOUT = 45.0          # 反证者只核对证据，给更短的时间
TOTAL_TIMEOUT = 90.0           # 一批侦察兵最长多久
OUT_CHARS = 600                # 每个子代理回传上限
TOTAL_CHARS = 1800             # 汇总给主模型的上限
TOOL_RESULT_CHARS = 3000       # 单个工具结果喂给子代理的上限
SEARCH_RESULT_CHARS = 9000     # 调研员单次搜索结果的回灌上限（要装得下正文片段）
FOLLOWUP_TTL = 600             # 子会话内存保留秒数（explore_followup 追问用）；0 = 关闭
MAX_BATCHES = 32               # 内存里最多保留几批子会话
WRITE_MAX = 8                  # 一批子代理最多写几次（防审批轰炸）
VERIFY_EVIDENCE = True         # 是否重新读取核对结论里的「文件:行号」

# 子会话缓存：batch_id -> {t, goal, user_goal, parent_brief, items:{id: {role, ask, messages}}}
# 内存只是热缓存；真正的恢复靠 data/subagents/<batch>.jsonl（见 _persist）。
_BATCHES = {}
_BATCH_LOCK = threading.Lock()

# 子代理允许写的工具：只给「建 / 改」，删除 / 移动仍留给主代理
SUB_WRITE_TOOLS = ("write_file", "edit_file", "make_dir")

ROLE_TOOLS = {
    "finder": ["list_dir", "search_code", "find_symbol", "repo_map", "code_context",
               "git_status", "git_log", "git_diff", "explore"],
    "reader": ["list_dir", "read_file", "search_files", "search_code", "code_context",
               "git_status", "git_log", "git_diff", "explore"],
    "worker": ["list_dir", "read_file", "search_files", "search_code",
               "find_symbol", "repo_map", "code_context",
               "write_file", "edit_file", "make_dir"],
    "critic": ["list_dir", "read_file", "search_files", "search_code", "code_context",
               "git_status", "git_diff"],
    # 外部资料调研：只上网，不碰本地代码；可以再派下级调研员
    "searcher": ["web_search", "explore"],
}

_COMMON = (
    "你是主代理派出的侦察兵。默认只能检索与阅读代码，不能执行命令、不能访问密钥文件。"
    "你的权限在分叉时已固定：越权操作一律被拒绝，不要重试，"
    "直接在结论里说明限制。只输出结论，不要复述文件原文。"
    "每条结论后用（文件:行号）标注证据，系统会重新读取核对；引用不存在会被打回。"
    "信息不足就写「未查清」，不要编造。结论控制在 600 字以内。"
    "最后一行固定写：`置信度: high|medium|low；未查清: ...`（没有就写「无」）。"
)
# 真 fork 的边界声明：父历史只是背景，子代理不要接着回答父对话
FORK_NOTE = (
    "下面是父会话到此刻为止的真实对话记录。你是从这段对话里分叉出来的侦察兵，"
    "不是主代理：不要接着回答父对话里的问题，也不要复述这些内容；"
    "读完最后给你的任务后，只围绕分配给你的问题检索与阅读。"
)
ROLE_PROMPTS = {
    "finder": _COMMON + "\n你的任务是**定位**：入口、定义、调用链。"
                        "优先用 search_code / find_symbol / repo_map，找到目标就停，不要通读整文件。",
    "reader": _COMMON + "\n你的任务是**精读**：围绕问题读取相关片段，提炼事实、分支、边界条件。"
                        "优先用 read_file 的 start/limit 只读相关段。",
    "worker": _COMMON + "\n你的任务是**落地**：既能检索阅读，也可以在沙箱内写文件。"
                        "每次写入都会经过用户审批；被拒绝就停下说明原因，不要换写法绕过。"
                        "改完要列出改了哪些文件、关键改动在哪几行。",
    "critic": (
        "你是反证者。**不要重新探索整个问题**，只核对其它侦察兵给出的结论与证据。"
        "逐条检查：引用的文件 / 行号是否真的存在；那几行是否真的支持该结论；"
        "有没有反例、边界条件或另一套实现；彼此之间是否矛盾。"
        "只能读代码，不能写文件 / 执行命令 / 读密钥。"
        "严格按下面的格式输出，总共不超过 500 字：\n"
        "结论：支持 / 反驳 / 无法判断\n"
        "依据：文件:行号\n"
        "修正：一句话（没有就写「无」）\n"
        "冲突：无，或写清哪两条互相矛盾"
    ),
    "searcher": (
        "你是主代理派出的**资料调研员**。你只能联网检索，不能读本地文件、不能写文件、不能执行命令。"
        "目标是把外部事实查准：规格、机制、参数、数据表、接口约定、参考实现。"
        "每条结论后面必须带上来源 URL（或台账编号 + URL），系统会核对 URL 是否真的来自本批搜索结果，"
        "编造链接会被打回。优先官方文档 / 规范 / 源码仓库 / 百科；论坛二手转述只作补充。"
        "关键参数尽量找第二个独立来源；两个来源冲突时把两边都写出来并注明冲突。"
        "查不到的不要猜：写「未查清」，并列出还缺哪几条信息。结论控制在 600 字以内，不要复述原文。"
    ),
}

# 子代理自己在结论里提到这些词，说明可能发现了矛盾 → 自动追加反证者
_CONFLICT_WORDS = ("冲突", "矛盾", "不一致", "对不上", "说法不同", "与…不符", "相反")



def _clip(text, n):
    t = str(text or "").strip()
    if len(t) <= n:
        return t
    return t[:max(0, n - 1)].rstrip() + "…"


def normalize_args(args, max_questions=MAX_QUESTIONS):
    """把模型给的工具参数收敛成结构；失败返回 (None, 原因)。"""
    args = args if isinstance(args, dict) else {}
    goal = _clip(args.get("goal") or "", 200)
    # 资料调研模式：只上网查外部事实，不写文件（与 allow_write 互斥）
    research = bool(args.get("research"))
    allow_write = bool(args.get("allow_write")) and not research
    qs = args.get("questions")
    if isinstance(qs, dict):
        qs = [qs]
    if not isinstance(qs, list) or not qs:
        return None, "缺少 questions：请给出 1–%d 个要查清的问题" % max_questions
    out = []
    for i, q in enumerate(qs[:max_questions]):
        if isinstance(q, str):
            ask, scope, hints = q.strip(), [], []
        elif isinstance(q, dict):
            ask = str(q.get("ask") or q.get("question") or "").strip()
            scope = [str(s).strip() for s in (q.get("scope") or []) if str(s).strip()]
            hints = [str(s).strip().replace("\\", "/")
                     for s in (q.get("hint_files") or []) if str(s).strip()]
        else:
            continue
        if not ask:
            continue
        out.append({"id": "q%d" % (i + 1), "ask": _clip(ask, 300),
                    "scope": scope[:4], "hint_files": hints[:6],
                    "role": "searcher" if research
                            else ("worker" if allow_write
                                  else ("finder" if i == 0 else "reader"))})
    if not out:
        return None, "questions 里没有可用的问题文本"
    acc = args.get("acceptance")
    if isinstance(acc, str):
        acc = [acc]
    acceptance = [_clip(a, 120) for a in (acc if isinstance(acc, list) else [])[:4]
                  if str(a or "").strip()]
    return {"goal": goal or out[0]["ask"], "questions": out,
            "verify": bool(args.get("verify")),
            "allow_write": allow_write,
            "research": research,
            "deliverable": _clip(args.get("deliverable") or "", 200),
            "acceptance": acceptance}, ""


def build_schemas(research=False):
    """子代理可用的工具 schema（原生 function calling 用）。

    research=True 时把 web_search 一并下发（资料调研模式）。
    """
    try:
        all_s = agent.native_tool_schemas(agent_on=True, search_ready=bool(research),
                                          cap_domains=set(), computer_on=False,
                                          lsp_on=False, git_commit_mode="off")
    except Exception:
        return []
    names = set()
    for tools in ROLE_TOOLS.values():
        names |= set(tools)
    return [s for s in all_s if (s.get("function") or {}).get("name") in names]


class Budget:
    """一批子代理共享的 token 预算；total=0 表示不限。"""

    def __init__(self, total=0):
        try:
            self.total = max(0, int(total or 0))
        except (TypeError, ValueError):
            self.total = 0
        self.used = 0
        self._lock = threading.Lock()

    def spend(self, n):
        try:
            n = max(0, int(n or 0))
        except (TypeError, ValueError):
            n = 0
        with self._lock:
            self.used += n

    def exhausted(self):
        with self._lock:
            return bool(self.total and self.used >= self.total)


class ReadCache:
    """同一轮里所有子代理共享的只读结果缓存：同一文件只读一次。"""

    def __init__(self, sandbox, max_entries=24):
        self.sandbox = sandbox
        self.max_entries = max(4, int(max_entries or 24))
        self._items = {}
        self._lock = threading.Lock()

    def call(self, name, args):
        key = (name, json.dumps(args or {}, sort_keys=True, ensure_ascii=False))
        with self._lock:
            if key in self._items:
                return self._items[key]
        text, _undo, _write = agent.run_tool(self.sandbox, name, args or {})
        with self._lock:
            self._items[key] = text
            while len(self._items) > self.max_entries:
                self._items.pop(next(iter(self._items)))
        return text


class Blackboard:
    """同一批侦察兵共享的「已定位文件」白板：三个人不重复找同一批文件。"""

    def __init__(self, max_files=24):
        self.max_files = max(4, int(max_files or 24))
        self.files = {}
        self.version = 0
        self._lock = threading.Lock()

    def note(self, name, args, out):
        text = str(out or "")
        changed = False
        with self._lock:
            if name == "read_file":
                p = str((args or {}).get("path") or "").replace("\\", "/").lstrip("./")
                if p and p not in self.files:
                    first = next((x.strip() for x in text.splitlines() if x.strip()), "")
                    self.files[p] = _clip(first, 50)
                    changed = True
            elif name in ("search_code", "search_files", "find_symbol", "repo_map"):
                for m in _PATH_HIT_RE.finditer(text):
                    p = m.group(1).replace("\\", "/").lstrip("./")
                    if p and p not in self.files:
                        self.files[p] = ""
                        changed = True
            elif name in ("write_file", "edit_file", "make_dir"):
                p = str((args or {}).get("path") or "").replace("\\", "/").lstrip("./")
                if p:
                    self.files[p] = "（本批已写入）"
                    changed = True
            if changed:
                while len(self.files) > self.max_files:
                    self.files.pop(next(iter(self.files)))
                self.version += 1
        return changed

    def brief(self, limit=8):
        with self._lock:
            items = list(self.files.items())[-limit:]
            ver = self.version
        if not items:
            return "", ver
        return ("队友已定位：" + "；".join(p + (("（" + s + "）") if s else "")
                                          for p, s in items)), ver


_PATH_HIT_RE = re.compile(
    r"([A-Za-z0-9_./\\-]+\.(?:py|js|ts|tsx|jsx|json|md|html|css|txt|yml|yaml|toml|ini|cfg))"
    r"(?::\d+)?")

# 证据：结论里的「文件:行号」
_EVIDENCE_RE = re.compile(r"([A-Za-z0-9_./\\-]+\.([A-Za-z0-9]{1,8})):(\d+)(?:-(\d+))?")
_IDENT_RE = re.compile(r"`([^`\n]{2,60})`|([A-Za-z_][A-Za-z0-9_]{2,40})")
_SKIP_WORDS = {"the", "and", "for", "with", "this", "that", "from", "app",
               "src", "def", "class", "import", "return", "true", "false", "none"}
_BATCH_ID_RE = re.compile(r"^ex_[0-9a-f]{8}$")


def _sandbox_path(sandbox, rel):
    root = os.path.abspath(sandbox or ".")
    full = os.path.abspath(os.path.join(root, str(rel or "")))
    root_n = os.path.normcase(root)
    if os.path.normcase(full) != root_n and not os.path.normcase(full).startswith(root_n + os.sep):
        return None
    return full


def _read_lines(full, start, end):
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None
    if start < 1 or start > len(lines):
        return []
    return lines[max(0, start - 1):min(len(lines), max(start, end))]


def _claims(text):
    """抽出结论里的「文件:行号」，同一文件同一行只算一次。"""
    out, seen = [], set()
    for m in _EVIDENCE_RE.finditer(str(text or "")):
        p = m.group(1).replace("\\", "/").lstrip("./")
        s = int(m.group(3))
        e = int(m.group(4) or s)
        if (p, s) in seen:
            continue
        seen.add((p, s))
        out.append({"path": p, "start": s, "end": max(s, e)})
        if len(out) >= 12:
            break
    return out


def _sentence_of(text, claim):
    pat = "%s:%d" % (claim["path"], claim["start"])
    for ln in str(text or "").splitlines():
        if pat in ln:
            return ln
    return ""


def _verify_evidence(text, sandbox):
    """重新读取被引用的行，核对引用是否真实存在、标识符是否对得上。"""
    res = {"checked": 0, "ok": 0, "missing": [], "weak": [], "score": 1.0}
    for c in _claims(text):
        res["checked"] += 1
        full = _sandbox_path(sandbox, c["path"])
        if full is None or not os.path.isfile(full):
            res["missing"].append("%s:%d" % (c["path"], c["start"]))
            continue
        rows = _read_lines(full, c["start"], c["end"])
        if not rows:
            res["missing"].append("%s:%d" % (c["path"], c["start"]))
            continue
        window = "".join(rows)
        sent = _sentence_of(text, c)
        toks = []
        for a, b in _IDENT_RE.findall(sent):
            t = (a or b).strip()
            if len(t) >= 3 and t.lower() not in _SKIP_WORDS:
                toks.append(t)
        if toks and not any(t in window for t in toks):
            res["weak"].append("%s:%d" % (c["path"], c["start"]))
            continue
        res["ok"] += 1
    if res["checked"]:
        res["score"] = round(res["ok"] / float(res["checked"]), 2)
    return res


_URL_RE = re.compile(r"https?://[^\s)<>\]\"'，。；]+")


def _verify_sources(text, urls):
    """核对结论里引用的 URL 是否真的来自本批搜索结果（防编造链接）。"""
    res = {"checked": 0, "ok": 0, "missing": [], "weak": [], "score": 1.0}
    known = {str(u).rstrip("/") for u in (urls or set())}
    for u in _URL_RE.findall(str(text or "")):
        u = u.rstrip(".,;").rstrip("/")
        res["checked"] += 1
        if u in known or any(u.startswith(k) or k.startswith(u) for k in known if k):
            res["ok"] += 1
        else:
            res["missing"].append(u)
    if res["checked"]:
        res["score"] = round(res["ok"] / float(res["checked"]), 2)
    return res


def _domains_of(text):
    out = set()
    for u in _URL_RE.findall(str(text or "")):
        m = re.match(r"https?://([^/]+)", u)
        if m:
            out.add(m.group(1).lower())
    return out


def _check_acceptance(acceptance, results, evidence_all=None):
    """可机检的验收条件；无法识别的规则只提示、不判失败。"""
    texts = [r.get("text") or "" for r in results if r.get("ok") and r.get("text")]
    joined = "\n".join(texts)
    files = set()
    for r in results:
        for c in _claims(r.get("text") or ""):
            files.add(c["path"])
    checks = []
    for rule in acceptance or []:
        rule = str(rule or "").strip()
        if not rule:
            continue
        low = rule.lower()
        ok, detail = True, ""
        m = re.match(r"^cites\s*>=\s*(\d+)$", low)
        if m:
            need = int(m.group(1))
            ok = len(files) >= need
            detail = "引用文件 %d/%d" % (len(files), need)
        else:
            m = re.match(r"^sources\s*>=\s*(\d+)$", low)
            if m:
                need = int(m.group(1))
                urls = set()
                for t in texts:
                    urls |= set(_URL_RE.findall(t))
                ok = len(urls) >= need
                detail = "引用来源 %d/%d" % (len(urls), need)
            elif re.match(r"^cross\s*>=\s*(\d+)$", low):
                need = int(re.match(r"^cross\s*>=\s*(\d+)$", low).group(1))
                doms = set()
                for t in texts:
                    doms |= _domains_of(t)
                ok = len(doms) >= need
                detail = "独立站点 %d/%d" % (len(doms), need)
            elif re.match(r"^answers\s*>=\s*(\d+)$", low):
                need = int(re.match(r"^answers\s*>=\s*(\d+)$", low).group(1))
                ok = len(texts) >= need
                detail = "有结论 %d/%d" % (len(texts), need)
            elif low.startswith("contains:"):
                need = rule.split(":", 1)[1].strip()
                ok = bool(need) and need in joined
                detail = "包含「%s」" % need
            elif low.startswith("not_contains:"):
                need = rule.split(":", 1)[1].strip()
                ok = bool(need) and need not in joined
                detail = "不含「%s」" % need
            elif low.startswith("evidence>="):
                try:
                    need = float(low.split(">=", 1)[1])
                except ValueError:
                    need = 0.8
                scores = [(r.get("evidence") or {}).get("score") for r in results]
                scores = [s for s in scores if isinstance(s, (int, float))]
                avg = round(sum(scores) / len(scores), 2) if scores else 0.0
                ok = avg >= need
                detail = "证据均分 %.2f/%.2f" % (avg, need)
            else:
                detail = "无法机检（已忽略）"
        checks.append({"rule": rule, "ok": ok, "detail": detail})
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


# ---------------------------------------------------------------- 子会话落盘

def _batch_dir():
    d = os.path.join(DATA_DIR, "subagents")
    os.makedirs(d, exist_ok=True)
    return d


def _batch_path(batch):
    if not _BATCH_ID_RE.match(str(batch or "")):
        return None
    return os.path.join(_batch_dir(), batch + ".jsonl")


def _persist(batch, rec):
    """整批快照追加一行；恢复时取最后一行（last-wins）。"""
    p = _batch_path(batch)
    if not p:
        return
    try:
        line = json.dumps({"t": rec.get("t"), "goal": rec.get("goal") or "",
                           "user_goal": rec.get("user_goal") or "",
                           "parent_brief": rec.get("parent_brief") or "",
                           "session": rec.get("session") or "",
                           "items": rec.get("items") or {}}, ensure_ascii=False)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print("[子代理] 子会话落盘失败：%s" % e)


def _load_batch(batch):
    p = _batch_path(batch)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        if not lines:
            return None
        rec = json.loads(lines[-1])
        return rec if isinstance(rec, dict) and rec.get("items") else None
    except (OSError, ValueError):
        return None


def _gc_files(ttl):
    """磁盘上的子会话按 mtime 清理：ttl*4（至少 1 小时）。"""
    try:
        d = _batch_dir()
        cutoff = time.time() - max(ttl * 4, 3600)
        for n in os.listdir(d):
            if not n.endswith(".jsonl"):
                continue
            p = os.path.join(d, n)
            try:
                if os.stat(p).st_mtime < cutoff:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _task_prompt(q, goal, user_goal, brief="", parent_brief="", deliverable=""):
    parts = []
    if parent_brief:
        # 轻量 fork：让子代理知道主对话做到哪了，避免它从零猜上下文；
        # 明确「只读参考、不要复述」，防止它把父对话当成要回答的问题。
        parts.append("父对话最近进展（只读参考，不要复述，也不要据此回答问题）：\n"
                     + parent_brief)
    parts += ["用户的原始请求（仅供参考，不要直接回答它）：" + (user_goal or "（略）"),
              "这一批探索的总目标：" + goal,
              "你负责的问题：" + q["ask"]]
    if q["scope"]:
        parts.append("优先范围：" + "、".join(q["scope"]))
    if q["hint_files"]:
        parts.append("已知线索（优先看这些）：" + "、".join(q["hint_files"]))
    if deliverable:
        parts.append("交付形态：" + deliverable)
    if q["role"] == "critic":
        parts.append("下面是其它侦察兵的结论，请核对它们引用的证据：\n" + (brief or "（没有结论）"))
        parts.append("只核对、不重新探索；严格按系统要求的四行格式输出，总共不超过 500 字。")
    elif q["role"] == "worker":
        parts.append("要求：可以检索与阅读；需要改代码就用 write_file / edit_file，"
                     "每次写入都会经过用户审批；被拒绝就停下说明，不要换写法绕过。"
                     "结论控制在 600 字以内，列出改了哪些文件、关键改动在哪些行。")
    elif q["role"] == "searcher":
        parts.append("要求：只用 web_search 联网查外部资料；不要读本地文件、不要写。"
                     "每条结论后面带来源 URL；找不到就写「未查清」并列出还缺什么。"
                     "结论控制在 600 字以内。")
    else:
        parts.append("要求：只做检索与阅读；不要写文件、不要执行命令；"
                     "结论控制在 600 字以内，每条结论后面标「文件:行号」；"
                     "查不到就直说，不要猜。")
    return "\n".join(parts)


def _result_brief(results):
    """给反证者看的结论清单（只保留结论，避免把整段原文再喂一遍）。"""
    lines = []
    for r in results or []:
        state = "" if r.get("ok") else "（%s）" % (r.get("error") or "未完成")
        lines.append("[%s/%s]%s %s" % (r.get("id") or "?", r.get("role") or "?",
                                       state, r.get("text") or "（无结论）"))
    return _clip("\n".join(lines), 2200)


def _looks_conflicted(results):
    for r in results or []:
        t = r.get("text") or ""
        if any(w in t for w in _CONFLICT_WORDS):
            return True
    return False


def _target_of(name, args):
    args = args if isinstance(args, dict) else {}
    for k in ("query", "pattern", "path", "name", "target"):
        v = args.get(k)
        if v:
            return _clip(v, 60)
    return ""


def _default_chat(ctx, messages, schemas, stop_event):
    """默认用主聊天模型跑子代理；thinking 关掉，省钱也更快。"""
    from app import llm
    return llm.stream_chat(
        ctx["api_key"], ctx["api_url"], ctx["model"], messages,
        think=thinking_payload(effective_think_mode(ctx["api_url"]), False,
                               ctx.get("think_level") or "high"),
        stop_event=stop_event, allow_tool=True, tools=schemas,
        max_tokens=SUB_MAX_TOKENS, timeout=(10, 45),
        idle_timeout=0, stall_timeout=0)


def _new_batch_id():
    return "ex_" + uuid.uuid4().hex[:8]


def _gc(ttl=None):
    """清掉过期的子会话；ttl<=0 表示追问关闭，全部清空。"""
    ttl = FOLLOWUP_TTL if ttl is None else max(0, int(ttl or 0))
    now = time.time()
    with _BATCH_LOCK:
        if ttl <= 0:
            _BATCHES.clear()
            return
        for k in [k for k, v in _BATCHES.items() if now - v["t"] > ttl]:
            _BATCHES.pop(k, None)
        while len(_BATCHES) > MAX_BATCHES:      # 超量：先丢最旧的
            oldest = min(_BATCHES, key=lambda k: _BATCHES[k]["t"])
            _BATCHES.pop(oldest, None)
    _gc_files(ttl)


def _remember(batch, goal, ctx, results, ttl):
    """把这一批子会话（含它们读过的工具结果）留下来，供 explore_followup 追问。"""
    items = {}
    for r in results or []:
        msgs = r.get("messages") or []
        if len(msgs) >= 2:
            items[r.get("id") or "?"] = {"role": r.get("role") or "reader",
                                         "ask": r.get("ask") or "",
                                         "messages": msgs}
    if not items:
        return
    rec = {"t": time.time(), "goal": goal,
           "user_goal": ctx.get("user_goal") or "",
           "parent_brief": ctx.get("parent_brief") or "",
           "session": ctx.get("session_id") or "",
           "research": bool(ctx.get("research")),
           "items": items}
    with _BATCH_LOCK:
        _BATCHES[batch] = rec
    _persist(batch, rec)
    _gc(ttl)


def _run_one(q, ctx, cache, budget, emit, stop_event, seed_messages=None):
    """跑一个侦察兵。seed_messages 非空 = 在同一子会话上继续追问。"""
    t0 = time.monotonic()
    role = q["role"]
    tools = ROLE_TOOLS[role]
    try:
        depth = max(0, int(ctx.get("depth") or 0))
    except (TypeError, ValueError):
        depth = 0
    try:
        max_depth = int(ctx.get("max_depth") if ctx.get("max_depth") is not None
                        else MAX_DEPTH)
        max_depth = max(0, max_depth)
    except (TypeError, ValueError):
        max_depth = MAX_DEPTH
    schemas = [s for s in ctx.get("schemas") or []
               if (s.get("function") or {}).get("name") in tools]
    if seed_messages:
        # 追问：沿用子会话的历史（含已读片段），换掉 system 以放宽读权限；
        # 调研员保持"只上网"的角色，别让它突然去读代码。
        messages = [{"role": "system",
                     "content": ROLE_PROMPTS["searcher" if role == "searcher" else "reader"]}]
        messages += [m for m in seed_messages if m.get("role") != "system"]
    else:
        fork = ctx.get("fork_prefix") or []
        messages = [{"role": "system", "content": ROLE_PROMPTS[role]}]
        if fork:
            # 真 fork：父会话的完整对话前缀 + 边界声明，再接本次任务
            messages.append({"role": "user", "content": FORK_NOTE})
            messages += [dict(m) for m in fork]
        messages.append({"role": "user", "content": _task_prompt(
            q, ctx["goal"], ctx.get("user_goal") or "",
            ctx.get("result_brief") or "",
            ctx.get("parent_brief") or "",
            ctx.get("deliverable") or "")})
    hard_timeout = CRITIC_TIMEOUT if role == "critic" else SUB_TIMEOUT
    emit({"type": "subagent_start", "id": q["id"], "role": role, "ask": q["ask"],
          "fork": bool(ctx.get("fork_prefix")) or bool(seed_messages),
          "prefix_tokens": int(ctx.get("fork_tokens") or 0)})

    def _write_tool(name, args):
        """写入走主循环的审批回调；本批次数用尽直接拒绝（0 = 不限）。"""
        raw = ctx.get("write_max")
        cap = int(WRITE_MAX if raw is None else raw)
        counter = ctx.get("write_count")
        if cap > 0 and counter is not None and counter[0] >= cap:
            return False, "本批写入已达上限 %d 次" % cap
        if counter is not None:
            counter[0] += 1
        fn = ctx.get("write_tool")
        if not callable(fn):
            return False, "写入未配置"
        try:
            return fn(name, args)
        except Exception as e:
            return False, "写入失败：%s" % e

    rounds, used, text, err, told_fallback = 0, [], "", "", False
    board = ctx.get("board")
    last_board = 0
    try:
        while rounds < MAX_SUB_ROUNDS:
            if stop_event is not None and stop_event.is_set():
                err = "用户已停止"
                break
            if budget.exhausted():
                err = "探索预算用完"
                break
            if time.monotonic() - t0 > hard_timeout:
                err = "超时"
                break
            rr = ctx["chat_fn"](ctx, messages, schemas, stop_event)
            rounds += 1
            budget.spend((rr.get("usage") or {}).get("total_tokens") or 0)
            if rr.get("native_fallback") and not told_fallback:
                told_fallback = True
                messages[0]["content"] += "\n\n" + agent.proto_block(False)
            calls = rr.get("tool_calls") or []
            if calls:
                if rr.get("visible"):
                    messages.append({"role": "assistant", "content": rr["visible"]})
                results = []
                for call in calls:
                    name = str((call or {}).get("tool") or "").strip()
                    args = (call or {}).get("args") or {}
                    if name not in tools:
                        results.append("【%s】不在你的可用范围内，请改用：%s"
                                       % (name, "、".join(tools)))
                        continue
                    emit({"type": "subagent_step", "id": q["id"], "tool": name,
                          "target": _target_of(name, args)})
                    used.append(name)
                    if name == "explore":
                        # 嵌套侦察：子代理可以再派下级；深度 / 总数 / 预算三重限制
                        if depth >= max_depth:
                            results.append("【explore】嵌套深度已到上限（%d 层），"
                                           "请直接给出结论。" % max_depth)
                            continue
                        raw_cap = ctx.get("max_total")
                        try:
                            cap = int(raw_cap) if raw_cap is not None else MAX_TOTAL
                        except (TypeError, ValueError):
                            cap = MAX_TOTAL
                        counter = ctx.get("spawn_count")
                        if cap > 0 and counter is not None and counter[0] >= cap:
                            results.append("【explore】本批子代理总数已到上限（%d 个）。" % cap)
                            continue
                        if counter is not None:
                            counter[0] += 1
                        sub_args = dict(args or {})
                        # 嵌套层默认只读；显式开启 nested_write 时仍走主循环审批
                        if not ctx.get("nested_write"):
                            sub_args["allow_write"] = False
                        sub_ctx = dict(ctx)
                        sub_ctx["depth"] = depth + 1
                        sub_ctx["budget_obj"] = budget
                        sub_ctx["allow_write"] = bool(ctx.get("nested_write"))
                        sub_ctx["emit"] = (lambda o, _d=depth + 1:
                                           emit(dict(o, depth=_d)))
                        sub_ctx["ttl"] = 0          # 嵌套批次不缓存，避免磁盘膨胀
                        sub_ctx.pop("schemas", None)   # 按子批的 research 重建
                        sub_ctx.pop("fork_prefix", None)
                        sub_ctx.pop("fork_tokens", None)
                        sub_ctx.pop("board", None)
                        sub_res = run(sub_args, sub_ctx)
                        if not sub_res.get("ok"):
                            results.append("【explore】%s"
                                           % (sub_res.get("error") or "子代理没有结果"))
                        else:
                            results.append("【explore】\n"
                                           + format_results(sub_res)[:TOOL_RESULT_CHARS])
                        continue
                    if name == "web_search":
                        fn = ctx.get("search_fn")
                        if not callable(fn):
                            results.append("【web_search】本批没有联网检索能力；"
                                           "请如实说明未查清，不要编造来源。")
                            continue
                        qs = args.get("queries") or []
                        try:
                            okw, out = fn(qs)
                        except Exception as e:
                            okw, out = False, "搜索失败：%s" % e
                        if not okw:
                            out = "❌ " + str(out)
                        results.append("【web_search】\n%s"
                                       % str(out)[:SEARCH_RESULT_CHARS])
                        continue
                    if name in SUB_WRITE_TOOLS:
                        if not ctx.get("allow_write"):
                            results.append("【%s】本批探索没有开启写入，请只做检索与阅读。" % name)
                            continue
                        lock = ctx.get("write_lock")
                        if lock is not None:
                            with lock:
                                okw, out = _write_tool(name, args)
                        else:
                            okw, out = _write_tool(name, args)
                        if not okw:
                            out = "❌ " + str(out)
                        if board:
                            board.note(name, args, out)
                        results.append("【%s】\n%s" % (name, str(out)[:TOOL_RESULT_CHARS]))
                        continue
                    try:
                        out = cache.call(name, args)
                    except Exception as e:
                        out = "工具执行失败：%s" % e
                    if board:
                        board.note(name, args, out)
                    results.append("【%s】\n%s" % (name, out[:TOOL_RESULT_CHARS]))
                body = ("工具执行结果如下，请据此继续；"
                        "查清后直接给结论，不要复述原文：\n\n" + "\n\n".join(results))
                if board:
                    brief, ver = board.brief()
                    if brief and ver != last_board:
                        last_board = ver
                        body += "\n\n【" + brief + "】"
                messages.append({"role": "user", "content": body})
                continue
            text = str(rr.get("visible") or "").strip()
            break
    except Exception as e:
        err = "子代理失败：%s" % e

    # 证据硬校验：代码场景核对「文件:行号」，调研场景核对来源 URL；对不上就重试一次
    evidence = None
    searcher = role == "searcher"
    verify_on = searcher or bool(ctx.get("verify_evidence", True))
    if text and not err and verify_on:
        def _verify(t):
            if searcher:
                return _verify_sources(t, ctx.get("web_urls") or set())
            return _verify_evidence(t, ctx.get("sandbox") or "")

        evidence = _verify(text)
        fixed = 0
        while (evidence.get("missing") and fixed < 1
               and rounds < MAX_SUB_ROUNDS
               and not (stop_event is not None and stop_event.is_set())
               and not budget.exhausted()
               and time.monotonic() - t0 <= hard_timeout):
            fixed += 1
            messages.append({"role": "assistant", "content": text})
            if searcher:
                fix_msg = ("以下来源 URL 不在本批搜索结果里：%s\n"
                           "只引用搜索实际返回过的链接；查不到就写「未查清」。"
                           % "、".join(evidence["missing"][:4]))
            else:
                fix_msg = ("以下引用无法对应到实际文件/行号：%s\n"
                           "请重新读取核对后重给结论；不要引用不存在的行，"
                           "也不要把整个文件贴回来。" % "、".join(evidence["missing"][:4]))
            messages.append({"role": "user", "content": fix_msg})
            try:
                rr = ctx["chat_fn"](ctx, messages, schemas, stop_event)
            except Exception as e:
                err = "证据重试失败：%s" % e
                break
            rounds += 1
            budget.spend((rr.get("usage") or {}).get("total_tokens") or 0)
            new_text = str(rr.get("visible") or "").strip()
            if not new_text:
                break
            text = new_text
            evidence = _verify(text)
        if evidence.get("missing") and not err:
            err = ("来源无法核对：%s" % "、".join(evidence["missing"][:3])) if searcher \
                else ("引用无法核对：%s" % "、".join(evidence["missing"][:3]))

    ms = int((time.monotonic() - t0) * 1000)
    text = _clip(text, OUT_CHARS)
    if not text and not err:
        err = "没有得出结论"
    ok = bool(text) and not err
    emit({"type": "subagent_done", "id": q["id"], "role": role, "ok": ok,
          "ms": ms, "rounds": rounds, "tools": len(used), "chars": len(text),
          "evidence": (evidence or {}).get("score"),
          "missing": len((evidence or {}).get("missing") or []),
          "error": err})
    return {"id": q["id"], "role": role, "ask": q["ask"], "ok": ok, "text": text,
            "error": err, "ms": ms, "rounds": rounds, "tools": used,
            "evidence": evidence, "messages": messages}


def _run_many(questions, ctx, cache, budget, emit, stop_event, seeds=None):
    """并发跑一批侦察兵；seeds 给定时按 id 复用已有子会话。"""
    t0 = time.monotonic()
    results = []
    if len(questions) == 1:
        q = questions[0]
        r = _run_one(q, ctx, cache, budget, emit, stop_event,
                     (seeds or {}).get(q["id"]))
        results.append(r)
        _notify_result(ctx, r)
        return results
    pool = ThreadPoolExecutor(max_workers=len(questions))
    try:
        futs = [pool.submit(_run_one, q, ctx, cache, budget, emit, stop_event,
                            (seeds or {}).get(q["id"])) for q in questions]
        remain = max(1.0, TOTAL_TIMEOUT - (time.monotonic() - t0))
        done, _pending = wait(futs, timeout=remain)
        for q, f in zip(questions, futs):
            if f in done:
                try:
                    r = f.result()
                    results.append(r)
                    _notify_result(ctx, r)
                    continue
                except Exception as e:
                    err_text = "失败：%s" % e
            else:
                err_text = "批次超时"
            emit({"type": "subagent_done", "id": q["id"], "role": q["role"],
                  "ok": False, "ms": int((time.monotonic() - t0) * 1000),
                  "rounds": 0, "tools": 0, "chars": 0, "error": err_text})
            failed = {"id": q["id"], "role": q["role"], "ask": q["ask"],
                      "ok": False, "text": "", "error": err_text,
                      "ms": 0, "rounds": 0, "tools": []}
            results.append(failed)
            _notify_result(ctx, failed)
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:          # Python < 3.9
            pool.shutdown(wait=False)
    return results


def run(args, ctx):
    """执行一批探索。ctx 需要 sandbox / api_key / api_url / model / emit / stop_event。"""
    try:
        depth = max(0, int(ctx.get("depth") or 0))
    except (TypeError, ValueError):
        depth = 0
    try:
        max_depth = int(ctx.get("max_depth") if ctx.get("max_depth") is not None
                        else MAX_DEPTH)
        max_depth = max(0, max_depth)
    except (TypeError, ValueError):
        max_depth = MAX_DEPTH
    if depth > max_depth:
        return {"ok": False, "error": "子代理嵌套深度已到上限（%d 层）" % max_depth,
                "results": [], "goal": "", "tokens": 0, "ms": 0, "batch": ""}
    limit = ctx.get("max_agents") or MAX_QUESTIONS
    try:
        limit = max(1, min(int(limit), MAX_QUESTIONS))
    except (TypeError, ValueError):
        limit = MAX_QUESTIONS
    norm, err = normalize_args(args, limit)
    if err:
        return {"ok": False, "error": err, "results": [], "goal": "",
                "tokens": 0, "ms": 0}
    ctx = dict(ctx)
    ctx["depth"] = depth
    ctx["max_depth"] = max_depth
    ctx["spawn_count"] = ctx.get("spawn_count") or [0]
    ctx["goal"] = norm["goal"]
    ctx["research"] = bool(norm.get("research"))
    if ctx["research"] and not callable(ctx.get("search_fn")):
        return {"ok": False, "error": "资料调研需要联网搜索，但本轮没有可用的搜索配置",
                "results": [], "goal": norm["goal"], "tokens": 0, "ms": 0}
    ctx["deliverable"] = norm.get("deliverable") or ""
    ctx.setdefault("chat_fn", _default_chat)
    ctx.setdefault("schemas", build_schemas(ctx["research"]))
    ctx.setdefault("web_urls", set())
    ctx.setdefault("board", Blackboard())
    ctx.setdefault("verify_evidence", VERIFY_EVIDENCE)
    ctx.setdefault("write_lock", threading.Lock())
    ctx.setdefault("write_count", [0])
    # 写入需要同时满足：模型要求写 + 设置允许 + 主循环提供了审批回调
    ctx["allow_write"] = bool(norm.get("allow_write")) and bool(ctx.get("allow_write", True)) \
        and callable(ctx.get("write_tool"))
    emit = ctx.get("emit") or (lambda o: None)
    budget = ctx.get("budget_obj") or Budget(ctx.get("budget") or 0)
    cache = ReadCache(ctx["sandbox"])
    questions = norm["questions"][:limit]
    stop_event = ctx.get("stop_event")
    t0 = time.monotonic()
    results = _run_many(questions, ctx, cache, budget, emit, stop_event)
    # 反证者：主代理明确要求（verify），或侦察兵自己提到矛盾时追加一个
    verify = bool(norm.get("verify")) or _looks_conflicted(results)
    if verify and any((r.get("text") or "").strip() for r in results):
        ctx["result_brief"] = _result_brief(results)
        critic_q = {"id": "critic", "ask": "核对以上结论与证据", "scope": [],
                    "hint_files": [], "role": "critic"}
        results.append(_run_one(critic_q, ctx, cache, budget, emit, stop_event))
    acceptance = _check_acceptance(norm.get("acceptance"), results)
    ms = int((time.monotonic() - t0) * 1000)
    emit({"type": "subagent_all", "n": len(results), "ms": ms,
          "tokens": budget.used, "ok": sum(1 for r in results if r["ok"]),
          "accept": acceptance.get("passed")})
    try:
        ttl = max(0, int(ctx.get("ttl") if ctx.get("ttl") is not None
                         else FOLLOWUP_TTL))
    except (TypeError, ValueError):
        ttl = FOLLOWUP_TTL
    batch = ""
    if ttl > 0:
        batch = _new_batch_id()
        _remember(batch, norm["goal"], ctx, results, ttl)
        with _BATCH_LOCK:
            if batch not in _BATCHES:       # 子代理全部失败：不给无法追问的批次号
                batch = ""
    return {"ok": any(r["ok"] for r in results), "results": results,
            "goal": norm["goal"], "tokens": budget.used, "ms": ms,
            "batch": batch, "ttl": ttl,
            "acceptance": acceptance,
            "writes": (ctx.get("write_count") or [0])[0],
            "verify": bool(any(r.get("role") == "critic" for r in results))}


def followup(args, ctx):
    """在已缓存的子会话上追问：复用它们读过的上下文，只补问一轮。

    args: {batch, questions:[{ask, id?}]}；id 指定追问哪个子代理（省略 = 轮流分配）。
    """
    args = args if isinstance(args, dict) else {}
    batch = str(args.get("batch") or "").strip()
    out = {"ok": False, "results": [], "goal": "", "batch": batch,
           "tokens": 0, "ms": 0}
    if not batch:
        out["error"] = "缺少 batch：用 explore 结果里给出的批次号"
        return out
    try:
        ttl = max(0, int(ctx.get("ttl") if ctx.get("ttl") is not None
                         else FOLLOWUP_TTL))
    except (TypeError, ValueError):
        ttl = FOLLOWUP_TTL
    if ttl <= 0:
        out["error"] = "子会话追问已关闭（agent_explore_ttl=0），请重新 explore"
        return out
    _gc(ttl)
    with _BATCH_LOCK:
        rec = _BATCHES.get(batch)
    if not rec:
        rec = _load_batch(batch)            # 进程重启后从磁盘冷恢复
        if rec:
            with _BATCH_LOCK:
                _BATCHES[batch] = rec
    if not rec:
        out["error"] = ("批次 %s 不存在（子会话文件已被清理），请重新 explore" % batch)
        return out
    qs = args.get("questions")
    if isinstance(qs, dict):
        qs = [qs]
    items = []
    for q in (qs if isinstance(qs, list) else [])[:MAX_QUESTIONS]:
        if isinstance(q, str):
            ask, target = q.strip(), ""
        elif isinstance(q, dict):
            ask = str(q.get("ask") or q.get("question") or "").strip()
            target = str(q.get("id") or q.get("target") or "").strip()
        else:
            continue
        if ask:
            items.append({"ask": _clip(ask, 300), "target": target})
    if not items:
        out["error"] = "缺少 questions：请给出要追问的问题"
        return out
    ctx = dict(ctx)
    ctx["goal"] = rec.get("goal") or "追问"
    ctx.setdefault("user_goal", rec.get("user_goal") or "")
    ctx.setdefault("parent_brief", rec.get("parent_brief") or "")
    ctx.setdefault("chat_fn", _default_chat)
    research = bool(rec.get("research"))
    ctx["research"] = research
    ctx.setdefault("schemas", build_schemas(research))
    ctx.setdefault("web_urls", set())
    ctx.setdefault("verify_evidence", VERIFY_EVIDENCE)
    ctx.setdefault("allow_write", False)
    emit = ctx.get("emit") or (lambda o: None)
    budget = Budget(ctx.get("budget") or 0)
    cache = ReadCache(ctx["sandbox"])
    stop_event = ctx.get("stop_event")
    keys = list(rec["items"].keys())
    seeds, questions = {}, []
    for i, it in enumerate(items):
        key = it["target"] if it["target"] in rec["items"] else keys[i % len(keys)]
        item = rec["items"][key]
        qid = "%s+%d" % (key, i + 1)
        item_role = "searcher" if item.get("role") == "searcher" else "reader"
        tail = ("只补充新增信息；仍按「结论 + 来源 URL」输出，600 字以内；查不到就说未查清。"
                if item_role == "searcher" else
                "只补充新增信息；仍按「结论 + 文件:行号」输出，600 字以内；查不到就说查不到。")
        seeds[qid] = item["messages"] + [{
            "role": "user",
            "content": ("追问（沿用你已读过的内容，不要从头重新探索）：" + it["ask"]
                        + "\n" + tail)}]
        questions.append({"id": qid, "ask": it["ask"], "scope": [],
                          "hint_files": [], "role": item_role})
    t0 = time.monotonic()
    results = _run_many(questions, ctx, cache, budget, emit, stop_event, seeds)
    ms = int((time.monotonic() - t0) * 1000)
    # 追问后的子会话继续留在同一批次里，可以接着问（TTL 续期）
    with _BATCH_LOCK:
        live = _BATCHES.get(batch)
        if live:
            live["t"] = time.time()
            for r in results:
                base = (r.get("id") or "").split("+")[0]
                if r.get("messages") and base in live["items"]:
                    live["items"][base] = {"role": r.get("role") or "reader",
                                           "ask": r.get("ask") or "",
                                           "messages": r["messages"]}
            _persist(batch, live)
    emit({"type": "subagent_all", "n": len(results), "ms": ms,
          "tokens": budget.used, "ok": sum(1 for r in results if r["ok"])})
    return {"ok": any(r["ok"] for r in results), "results": results,
            "goal": rec.get("goal") or "", "tokens": budget.used, "ms": ms,
            "batch": batch, "ttl": ttl}


def format_followup(res):
    """追问结果压成给主模型看的一段文本。"""
    res = res or {}
    if not res.get("results"):
        return "【追问】%s" % (res.get("error") or "没有结果")
    lines = ["【子代理追问】%s（%d 条 · %d token · %.1fs）"
             % (res.get("batch") or "", len(res["results"]),
                res.get("tokens") or 0, (res.get("ms") or 0) / 1000.0)]
    for r in res["results"]:
        state = "" if r.get("ok") else "（%s）" % (r.get("error") or "未完成")
        lines.append("[%s]%s %s" % (r.get("id") or "?", state,
                                    r.get("text") or "（无结论）"))
    return _clip("\n".join(lines), TOTAL_CHARS)


def format_results(res):
    """压成给主模型看的一段文本（≤1800 字）。"""
    res = res or {}
    if not res.get("results"):
        return "【探索】%s" % (res.get("error") or "没有结果")
    critic = next((r for r in res["results"] if r.get("role") == "critic"), None)
    warn = ""
    if critic:
        ct = critic.get("text") or ""
        if ("反驳" in ct or "无法判断" in ct
                or ("冲突：" in ct and "冲突：无" not in ct)):
            warn = "⚠ 复核发现反驳或冲突，先看 [critic] 再行动。\n"
    lines = [warn + "【探索小队】%s（%d 个子代理 · %d token · %.1fs%s）"
             % (res.get("goal") or "", len(res["results"]),
                res.get("tokens") or 0, (res.get("ms") or 0) / 1000.0,
                (" · 写入 %d 次" % res["writes"]) if res.get("writes") else "")]
    for r in res["results"]:
        state = "" if r.get("ok") else "（%s）" % (r.get("error") or "未完成")
        ev = r.get("evidence") or {}
        tag = ""
        if ev.get("checked"):
            tag = "（证据 %d/%d" % (ev.get("ok") or 0, ev.get("checked"))
            if ev.get("weak"):
                tag += "，%d 条待核对" % len(ev["weak"])
            tag += "）"
        lines.append("[%s/%s]%s %s%s" % (r.get("id") or "?", r.get("role") or "?",
                                         state, r.get("text") or "（无结论）", tag))
    acc = res.get("acceptance") or {}
    if acc.get("checks"):
        if acc.get("passed"):
            lines.append("验收：通过（%s）" % "；".join(
                "%s %s" % (c.get("rule"), c.get("detail")) for c in acc["checks"]))
        else:
            lines.append("⚠ 验收未过：" + "；".join(
                "%s ✗ %s" % (c.get("rule"), c.get("detail"))
                for c in acc["checks"] if not c.get("ok")))
    if res.get("batch"):
        lines.append("（批次 %s｜%d 秒内可用 explore_followup 直接追问细节，别重新探索）"
                     % (res["batch"], res.get("ttl") or 0))
    lines.append("提示：以上结论都带文件:行号；需要展开某个文件时再用 read_file 精读。")
    return _clip("\n".join(lines), TOTAL_CHARS)


# ============================================================
# 后台批次：启动后立即返回，status / result / cancel 取用
# ============================================================

_JOBS = {}
_JOB_LOCK = threading.Lock()


class _CombinedEvent:
    """同时监听用户停止与后台取消。"""

    def __init__(self, *events):
        self.events = [e for e in events if e is not None]

    def is_set(self):
        return any(e.is_set() for e in self.events)


def _notify_result(ctx, r):
    cb = ctx.get("on_result")
    if callable(cb):
        try:
            cb(r)
        except Exception:
            pass


def _gc_jobs(ttl):
    if ttl <= 0:
        return
    now = time.time()
    with _JOB_LOCK:
        stale = [b for b, j in _JOBS.items()
                 if j.get("finished") and now - j["finished"] > ttl]
        for b in stale:
            _JOBS.pop(b, None)


def start_background(args, ctx):
    """启动一批后台侦察，立即返回批次号；结果用 status / result 取。"""
    try:
        cap = int(ctx.get("background_max")
                  if ctx.get("background_max") is not None else 0)
    except (TypeError, ValueError):
        cap = 0
    with _JOB_LOCK:
        running = sum(1 for j in _JOBS.values() if j.get("status") == "running")
        if cap > 0 and running >= cap:
            return {"ok": False, "error": "后台子代理并发已满（%d）" % cap}
    norm, _err = normalize_args(args, MAX_QUESTIONS)
    batch = _new_batch_id()
    cancel = threading.Event()
    rec = {"batch": batch, "status": "running", "started": time.time(),
           "finished": 0.0, "result": None, "error": "",
           "cancel": cancel, "partial": [], "done": 0,
           "total": len(norm["questions"]) if norm else 0}
    with _JOB_LOCK:
        _JOBS[batch] = rec
    emit = ctx.get("emit") or (lambda o: None)
    parent_stop = ctx.get("stop_event")

    def _on_result(r):
        with _JOB_LOCK:
            rec["done"] += 1
            rec["partial"] = (rec["partial"] + [{
                "id": r.get("id"), "role": r.get("role"),
                "ok": bool(r.get("ok")),
                "text": _clip(r.get("text") or "", 300)}])[-10:]

    def _run():
        def _emit_safe(o):
            try:
                emit(o)
            except Exception:
                pass           # 主回合结束后 SSE 连接已关闭，后台事件不能把线程带崩
        try:
            sub_ctx = dict(ctx)
            sub_ctx["stop_event"] = _CombinedEvent(cancel, parent_stop)
            sub_ctx["on_result"] = _on_result
            sub_ctx["emit"] = _emit_safe
            sub_ctx.pop("schemas", None)
            res = run(args, sub_ctx)
            rec["result"] = res
            if rec["status"] != "cancelled":     # 取消状态不被收尾覆盖
                rec["status"] = "done" if res.get("ok") else "failed"
                rec["error"] = "" if res.get("ok") else str(res.get("error") or "")
        except Exception as e:
            rec["status"] = "failed"
            rec["error"] = str(e)
        finally:
            rec["finished"] = time.time()
            try:
                emit({"type": "explore_done", "batch": batch,
                      "status": rec["status"],
                      "ms": int((rec["finished"] - rec["started"]) * 1000)})
            except Exception:
                pass
            try:
                _gc_jobs(int(ctx.get("background_ttl") or 0))
            except (TypeError, ValueError):
                pass

    threading.Thread(target=_run, name="explore-bg-" + batch, daemon=True).start()
    return {"ok": True, "batch": batch, "status": "running"}


def status(batch):
    """查看后台批次：状态 + 已完成的部分结果。"""
    batch = str(batch or "").strip()
    if not _BATCH_ID_RE.match(batch):
        return {"ok": False, "error": "批次号不合法：%r" % batch}
    with _JOB_LOCK:
        rec = _JOBS.get(batch)
        snap = None
        if rec:
            snap = {"status": rec["status"], "done": rec["done"],
                    "total": rec["total"], "partial": list(rec["partial"]),
                    "error": rec["error"],
                    "ms": int(((rec["finished"] or time.time())
                               - rec["started"]) * 1000)}
    if snap:
        return dict({"ok": True, "batch": batch}, **snap)
    disk = _load_batch(batch)
    if disk:
        return {"ok": True, "batch": batch, "status": "done", "done": 0,
                "total": len(disk.get("items") or {}), "partial": [],
                "ms": 0, "error": ""}
    return {"ok": False, "error": "找不到批次 %s" % batch}


def result(batch):
    """取后台批次的完整汇总；还在跑就返回 running。"""
    batch = str(batch or "").strip()
    if not _BATCH_ID_RE.match(batch):
        return {"ok": False, "error": "批次号不合法：%r" % batch}
    with _JOB_LOCK:
        rec = _JOBS.get(batch)
        if rec and rec["status"] == "running":
            return {"ok": False, "status": "running",
                    "error": "批次还在运行；先用 explore_status 看进度"}
        res = rec["result"] if rec else None
        st = rec["status"] if rec else ""
        err = rec["error"] if rec else ""
    if not res:
        disk = _load_batch(batch)
        if disk:
            items = []
            for k, v in (disk.get("items") or {}).items():
                text = ""
                for m in reversed(v.get("messages") or []):
                    if m.get("role") == "assistant" and str(m.get("content") or "").strip():
                        text = str(m.get("content") or "")
                        break
                items.append({"id": k, "role": v.get("role") or "reader",
                              "ask": v.get("ask") or "", "ok": bool(text),
                              "text": text})
            res = {"ok": bool(items), "results": items,
                   "goal": disk.get("goal") or "", "tokens": 0, "ms": 0,
                   "batch": batch}
            st = "done"
        else:
            return {"ok": False, "status": st or "unknown",
                    "error": err or "找不到批次 %s" % batch}
    return {"ok": True, "status": st or "done",
            "batch": res.get("batch") or batch,
            "text": format_results(res),
            "tokens": res.get("tokens") or 0, "ms": res.get("ms") or 0}


def cancel(batch):
    """取消还在跑的后台批次；已完成/已取消的返回原因。"""
    batch = str(batch or "").strip()
    if not _BATCH_ID_RE.match(batch):
        return {"ok": False, "error": "批次号不合法：%r" % batch}
    with _JOB_LOCK:
        rec = _JOBS.get(batch)
        if not rec:
            return {"ok": False, "error": "找不到批次 %s" % batch}
        if rec["status"] != "running":
            return {"ok": False, "error": "批次已经结束（%s）" % rec["status"]}
        rec["status"] = "cancelled"
        try:
            rec["cancel"].set()
        except Exception:
            pass
    return {"ok": True, "batch": batch, "status": "cancelled"}


def list_jobs():
    now = time.time()
    with _JOB_LOCK:
        return [{"batch": b, "status": j["status"],
                 "done": j["done"], "total": j["total"],
                 "ms": int(((j["finished"] or now) - j["started"]) * 1000)}
                for b, j in _JOBS.items()]


def format_jobs():
    """给主模型的一行后台批次摘要（不注入结果，只提示去取）。"""
    jobs = list_jobs()
    if not jobs:
        return ""
    parts = []
    for j in jobs[-6:]:
        extra = " %d/%d" % (j["done"], j["total"]) if j["total"] else ""
        parts.append("%s %s%s" % (j["batch"], j["status"], extra))
    return ("后台子代理：" + "；".join(parts)
            + "（用 explore_status / explore_result 取，取消用 explore_cancel）")
