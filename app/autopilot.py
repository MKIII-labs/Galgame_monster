# -*- coding: utf-8 -*-
"""自动化（Autopilot）：一次输入之后，由「老板」持续规划、「员工」执行、「裁判」验收。

三层分工：
- **老板**（LLM，同一个模型）：只决定「说什么」—— 下一轮指令 / 换计划 / 提议收工 / 需要人；
- **裁判**（确定性检查 + 独立评审员）：只决定「算不算完」—— 命令退出码 / 文件存在性 /
  没有可机检项时，由新上下文的评审员只看证据逐条判分；
- **调度**（本模块的线程）：只决定「停不停」—— 无进展看门狗 / 预算 / 暂停 / 抢占。

为什么老板不能兼任裁判：员工说「做完了」，老板只看得到这句话，就会同意；
同源偏差会让「完成」变成自我声明。老板的输入刻意只给 ledger（工具次数、失败、
退出码）和裁判结论，不给员工的长篇自述，同一个模型在不同视角下才会给出不同判断。

中断恢复：
- 每回合前把「目标 / 计划 / 断点 / 上次验收 / 未决问题」固定注入员工系统提示；
- 瞬态错误（网络 / 超时 / 限流 / 空回合）自动退避重试，不暂停目标；
- 进程重启后 `recover()` 把未完成目标接回线程；看门狗兜住线程意外退出。

驱动方式：每回合在进程内直接调用 ``api.chat._api_chat``（一个假 handler），
不走网络、不解析 SSE。依据是它只依赖 _body / send_response / send_header /
end_headers / wfile.write / flush / _json 这几个方法。
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from app import agent, events, llm
from app.autonomy_policy import normalize_mode
from app.config import get_api_conf, load_config
from app.store import DATA_DIR, read_json, write_json

GOALS_FILE = os.path.join(DATA_DIR, "goals.json")

MAX_TURNS = 30                 # 兼容旧配置；只有 autopilot_use_budget 打开时才当硬上限
MAX_MINUTES = 90               # 同上
BOSS_MAX_TOKENS = 900          # 老板每次决策的输出上限
BOOTSTRAP_MAX_TOKENS = 1800    # 立项书（完成定义 + 计划）的输出上限
REVIEW_MAX_TOKENS = 700        # 完成定义补漏（防半成品）的输出上限
VERIFY_MAX_TOKENS = 1200       # 独立评审员的输出上限
TURN_TEXT_LIMIT = 600          # 留给老板看的「员工最后一段话」截断长度
CRITERIA_CMD_TIMEOUT = 180     # 验收命令的单条超时（秒）
LEDGER_KEEP = 12               # 给老板看最近几轮证据
DEFAULT_NO_PROGRESS = 4        # 连续几回合没有实际进展就干预
RETRY_BASE = 5                 # 瞬态错误退避起点（秒）
RETRY_CAP = 60                 # 退避上限（秒）
START_COOLDOWN = 30            # 看门狗重启同一目标的最小间隔（秒）
STALE_PAUSED_SECONDS = 6 * 3600   # 从未跑过的暂停目标超过这么久，视为历史

_LOCK = threading.RLock()
_THREADS = {}                  # gid -> Thread
_STOP = {}                     # gid -> Event（暂停 / 停止用）
_STARTED = {}                  # gid -> 最近一次起线程的时间（看门狗冷却）
_WATCHDOG = None
_WATCHDOG_LOCK = threading.Lock()


def _now():
    return time.time()


def _new_id():
    return "g_" + uuid.uuid4().hex[:10]


# ------------------------------------------------------------
# 存取
# ------------------------------------------------------------
def load_goals():
    data = read_json(GOALS_FILE, None)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        data = {"items": []}
    return data


def save_goals(data):
    write_json(GOALS_FILE, data, indent=None)


def list_goals():
    with _LOCK:
        return list(load_goals()["items"])


def get_goal(gid):
    for g in list_goals():
        if g.get("id") == gid:
            return g
    return None


def goal_for_session(sid):
    """取这个会话当前还在跑 / 需要你 / 暂停中的目标（前端状态条用）。

    只返回 active 的目标：历史遗留的 paused / done / stopped 不能占住按钮。
    """
    rows = [g for g in list_goals()
            if g.get("session_id") == sid and _goal_active(g)]
    if not rows:
        return None
    rows.sort(key=lambda g: float(g.get("created_at") or 0), reverse=True)
    return rows[0]


def _update(gid, **fields):
    with _LOCK:
        data = load_goals()
        hit = next((g for g in data["items"] if g.get("id") == gid), None)
        if hit is None:
            return None
        hit.update(fields)
        hit["updated_at"] = _now()
        save_goals(data)
        return hit


def _publish(goal):
    """把目标的最新状态推给前端（状态条 / 通知都靠它）。"""
    if not goal:
        return
    slim = {k: goal.get(k) for k in (
        "id", "session_id", "objective", "state", "active", "turn", "max_turns",
        "last_reason", "last_error", "pause_reason", "no_progress", "retries")}
    slim["criteria"] = goal.get("criteria") or []
    slim["verdict"] = _verdict_summary(goal.get("verdict") or [])
    slim["updated_at"] = goal.get("updated_at")
    try:
        events.publish({"type": "autopilot", "goal": slim})
    except Exception:
        pass


# ------------------------------------------------------------
# 实时看板：回合内的工具调用 / 阶段推进
# ------------------------------------------------------------
# 为什么需要：回合由服务端自己跑（_run_turn 用 _FakeHandler 调 chat），
# SSE 帧全被吞进内存，前端在一整个回合里（可能几分钟）看不到任何东西。
# 这里把关键帧转成 autopilot_live 事件推给前端看板；纯展示，不进状态机。
_LIVE_ARG_KEYS = ("path", "to", "from", "cmd", "command", "pattern", "query",
                  "url", "name", "file", "dir")


def _slim_args(args):
    """工具参数只留可读短字段：文件内容 / 长文本一律不推。"""
    if not isinstance(args, dict):
        return {}
    out = {}
    for k in _LIVE_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            out[k] = str(v)[:120]
    if out:
        return out
    for k, v in list(args.items())[:4]:
        if isinstance(v, (str, int, float)) and 0 < len(str(v)) <= 120:
            out[k] = str(v)
    return out


def _push_live(g, kind, **fields):
    """推一条实时进度。失败静默：看板挂了不能影响自动化本身。"""
    if not isinstance(g, dict):
        return
    obj = {"type": "autopilot_live", "kind": kind,
           "goal_id": g.get("id"), "session_id": g.get("session_id"),
           "at": _now()}
    obj.update(fields)
    try:
        events.publish(obj)
    except Exception:
        pass


def _phase(g, phase, text="", **fields):
    """阶段推进：立项 / 执行 / 机检 / 评审 / 决策。看板顶栏直接显示。"""
    _push_live(g, "phase", phase=str(phase or ""), text=str(text or "")[:240], **fields)


# ------------------------------------------------------------
# 兼容 / 迁移
# ------------------------------------------------------------
def _goal_active(g):
    """这个目标还算「当前目标」吗？

    旧版 paused 目标没有 pause_reason，只是历史遗留；如果把它们当成当前目标，
    会话一切换就会显示「已暂停」，点恢复还会把很久以前的 objective 再发一遍。
    另外「从没真正跑过（turn=0）且创建很久」的暂停目标也按历史处理。
    """
    if not isinstance(g, dict):
        return False
    if "active" in g and not g.get("active"):
        return False
    st = str(g.get("state") or "")
    pr = str(g.get("pause_reason") or "")
    if st in ("running", "retrying", "needs_human"):
        return True
    if st not in ("paused", "failed"):
        return False
    # 旧版 paused / failed 没有 pause_reason：历史遗留
    if "active" not in g and not pr:
        return False
    try:
        turn = int(g.get("turn") or 0)
    except (TypeError, ValueError):
        turn = 0
    try:
        created = float(g.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0
    if turn <= 0 and created and (_now() - created) > STALE_PAUSED_SECONDS:
        return False
    return True


def _migrate_goal(g):
    """把旧版目标补齐到 v2；只改内存对象，落盘由调用方决定。"""
    if not isinstance(g, dict):
        return g
    if int(g.get("schema") or 1) < 2:
        g["schema"] = 2
    if "active" not in g:
        g["active"] = _goal_active(g)
    if "definition" not in g or not isinstance(g.get("definition"), dict):
        g["definition"] = _definition_from_criteria(g.get("criteria") or [])
    for key, default in (("decisions", []), ("open_questions", []),
                         ("checkpoint", {}), ("verdict", []),
                         ("no_progress", 0), ("retries", 0),
                         ("progress_sig", ""), ("pause_reason", ""),
                         ("unknown_streak", 0), ("bootstrap_tries", 0)):
        if key not in g:
            g[key] = default
    if not g.get("last_progress_at"):
        g["last_progress_at"] = float(g.get("started_at") or _now())
    if not g.get("started_at"):
        g["started_at"] = _now()
    return g


# ------------------------------------------------------------
# 完成定义（DoD）
# ------------------------------------------------------------
def _definition_from_criteria(criteria):
    """旧版 criteria → 新版 definition：有 cmd 的进机检，没 cmd 的进 rubric。"""
    checks, rubric = [], []
    for c in criteria or []:
        if isinstance(c, str):
            c = {"text": c, "cmd": ""}
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        cmd = str(c.get("cmd") or "").strip()
        if not text and not cmd:
            continue
        if cmd:
            checks.append({"id": "c%d" % len(checks), "text": text or cmd,
                           "kind": "command", "cmd": cmd,
                           "source": c.get("source") or "user"})
        else:
            rubric.append({"id": "r%d" % len(rubric), "text": text or "完成标准",
                           "evidence": "文件、运行输出、截图或可复现的验证步骤",
                           "source": c.get("source") or "user"})
    return {"deliverables": [], "checks": checks, "rubric": rubric}


def _empty_definition():
    return {"deliverables": [], "checks": [],
            "rubric": [{"id": "r0", "text": "目标整体达成",
                        "evidence": "交付物、运行输出、截图或可复现的验证步骤",
                        "source": "system"}]}


def _norm_definition(raw):
    """把模型返回的完成定义规整成 {deliverables, checks, rubric}。"""
    if not isinstance(raw, dict):
        raw = {}
    out = {"deliverables": [], "checks": [], "rubric": []}

    for i, it in enumerate((raw.get("deliverables") or [])[:12], 1):
        if isinstance(it, str):
            it = {"name": it, "path": it if re.search(r"[./\\]", it) else ""}
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("text") or "").strip()
        path = str(it.get("path") or "").strip()
        if not name and not path:
            continue
        out["deliverables"].append({
            "id": "d%d" % i, "name": name or path, "path": path,
            "kind": str(it.get("kind") or "file").strip() or "file",
        })

    for it in (raw.get("checks") or [])[:10]:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or it.get("name") or "").strip()
        cmd = str(it.get("cmd") or it.get("command") or "").strip()
        path = str(it.get("path") or "").strip()
        url = str(it.get("url") or "").strip()
        kind = str(it.get("kind") or "").strip().lower()
        if not kind:
            kind = "command" if cmd else ("http" if url else ("file" if path else "rubric"))
        if kind == "rubric":
            if text:
                out["rubric"].append({"id": "r%d" % (len(out["rubric"]) + 1),
                                      "text": text,
                                      "evidence": "文件、运行输出、截图或可复现的验证步骤",
                                      "source": "boss"})
            continue
        if not text:
            text = cmd or path or url
        if not (cmd or path or url):
            continue
        out["checks"].append({
            "id": "c%d" % (len(out["checks"]) + 1), "text": text,
            "kind": kind if kind in ("command", "file", "http") else "command",
            "cmd": cmd, "path": path, "url": url,
            "source": it.get("source") or "boss",
        })

    for it in (raw.get("rubric") or [])[:10]:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or it.get("name") or "").strip()
        if not text:
            continue
        out["rubric"].append({
            "id": "r%d" % (len(out["rubric"]) + 1), "text": text,
            "evidence": str(it.get("evidence") or
                            "文件、运行输出、截图或可复现的验证步骤").strip(),
            "source": it.get("source") or "boss",
        })

    if not (out["deliverables"] or out["checks"] or out["rubric"]):
        return _empty_definition()
    # 交付物没给路径时无法机检：把它转成一条 rubric，交给评审员看证据，
    # 而不是直接给 ok=None 卡死整个目标（bootstrap 允许「路径不确定」）。
    for d in out["deliverables"]:
        if not d.get("path"):
            out["rubric"].append({
                "id": "r%d" % (len(out["rubric"]) + 1),
                "text": "交付物「%s」已产出且可用" % (d.get("name") or "交付物"),
                "evidence": "文件 / 入口 / 运行输出 / 截图",
                "source": "boss",
            })
    return out


def _has_definition(g):
    d = g.get("definition") or {}
    return bool((d.get("deliverables") or d.get("checks") or d.get("rubric"))
                or (g.get("criteria") or []))


def _criteria_from_definition(definition):
    """把定义映射回旧 criteria（前端 / 兼容读取用）。"""
    out = []
    for c in (definition or {}).get("checks") or []:
        out.append({"text": c.get("text") or c.get("cmd") or "",
                    "cmd": c.get("cmd") or "", "source": c.get("source") or "boss"})
    for r in (definition or {}).get("rubric") or []:
        out.append({"text": r.get("text") or "", "cmd": "",
                    "source": r.get("source") or "boss"})
    return out


# ------------------------------------------------------------
# 目标摘要（每回合固定注入员工）
# ------------------------------------------------------------
def _definition_lines(definition):
    lines = []
    dels = (definition or {}).get("deliverables") or []
    if dels:
        lines.append("交付物：")
        for d in dels[:10]:
            tail = ("｜路径：" + d["path"]) if d.get("path") else "｜路径未定"
            lines.append("- " + (d.get("name") or "交付物") + tail)
    checks = (definition or {}).get("checks") or []
    if checks:
        lines.append("机检验收（由系统执行）：")
        for c in checks[:10]:
            if c.get("kind") == "command":
                tail = "｜命令：" + (c.get("cmd") or "")
            elif c.get("kind") == "file":
                tail = "｜文件：" + (c.get("path") or "")
            elif c.get("kind") == "http":
                tail = "｜URL：" + (c.get("url") or "")
            else:
                tail = ""
            lines.append("- " + (c.get("text") or "") + tail)
    rubric = (definition or {}).get("rubric") or []
    if rubric:
        lines.append("评审验收（无命令时由独立评审员判定）：")
        for r in rubric[:10]:
            lines.append("- " + (r.get("text") or "") +
                         ("｜需要证据：" + str(r.get("evidence")) if r.get("evidence") else ""))
    return lines


def _goal_brief(g):
    """每回合固定注入员工系统提示的目标块（不能只靠历史，压缩后会被压掉）。"""
    lines = ["## 自动化目标（每次请求固定注入）",
             "目标：" + (g.get("objective") or "")]
    cons = g.get("constraints") or []
    if cons:
        lines.append("用户中途追加的约束（必须遵守）：")
        lines.extend("- " + c for c in cons[:8])
    crit = g.get("criteria") or []
    dlines = _definition_lines(g.get("definition") or {})
    # 新目标会把 criteria 映射进 definition；两者都写会重复，只在没有定义时回退到旧字段。
    if crit and not dlines:
        lines.append("验收标准（由系统执行，不是你自己宣布）：")
        for c in crit[:6]:
            tail = ("｜命令：" + c["cmd"]) if c.get("cmd") else "｜不可自动验收"
            lines.append("- " + (c.get("text") or "") + tail)
    if dlines:
        lines.append("完成定义（由系统验收，不是你自己宣布）：")
        lines.extend(dlines[:20])
    plan = g.get("plan") or []
    if plan:
        lines.append("当前计划：")
        lines.extend("%d. %s" % (i + 1, str(p)[:120]) for i, p in enumerate(plan[:10]))
    ck = g.get("checkpoint") or {}
    if ck:
        lines.append("断点（续跑时先核对现状，不要重复已完成的写入）：")
        if ck.get("turn"):
            lines.append("- 上次完成回合：%s" % ck["turn"])
        if ck.get("files"):
            lines.append("- 上次改过的文件：" + "、".join(ck["files"][:8]))
        if ck.get("verdict"):
            lines.append("- 上次验收：" + str(ck["verdict"])[:300])
        if ck.get("pending"):
            lines.append("- 上次待办：" + str(ck["pending"])[:300])
        if ck.get("pending_tools"):
            lines.append("上次有工具执行到一半，结果未知：")
            lines.extend("- " + str(x)[:160] for x in (ck.get("pending_tools") or [])[:8])
            lines.append("这些调用可能有副作用：不要直接重试；先读文件 / 查状态 / 看输出，"
                         "确认外部状态后再决定是否重做。")
    if g.get("open_questions"):
        lines.append("未决问题（能自决就自决，不能就把缺口说清楚）：")
        lines.extend("- " + str(q)[:160] for q in (g.get("open_questions") or [])[:5])
    mode = normalize_mode(
        g.get("question_policy")
        or (load_config() or {}).get("autonomy_question_policy"))
    if mode == "auto":
        lines.append("提问策略：auto。遇到问题自己按目标做决定，不要等用户；"
                     "不可逆操作也不要执行，改用可回滚的最小方案。")
    elif mode == "smart":
        lines.append("提问策略：smart。可逆 / 低风险自己决定；"
                     "删除、覆盖、发布、付款、改凭据或缺少外部信息时才调用 ask_user。")
    else:
        lines.append("提问策略：ask。需要拍板时调用 ask_user，把选项摆清楚。")
    lines.append(
        "纪律：自主推进到底，不要问「要不要继续」；能用工具验证的就去验证，"
        "不要自称完成。确实需要人拍板（不可逆、涉及偏好、缺外部信息）时停下来说明。")
    return "\n".join(lines)


# ------------------------------------------------------------
# 创建 / 控制
# ------------------------------------------------------------
def create(session_id, objective, criteria=None, max_turns=None, max_minutes=None,
           question_policy=None):
    """建一个目标并立刻开跑。返回 goal 或 {"error": ...}。"""
    from app.sessions import find_session, load_sessions

    sid = str(session_id or "").strip()
    text = str(objective or "").strip()
    if not sid:
        return {"error": "缺少 session_id"}
    if not text:
        return {"error": "目标为空：先输入你想让它做完的事"}
    s = find_session(load_sessions(), sid)
    if not s:
        return {"error": "会话不存在"}
    if not s.get("agent_enabled") or not s.get("sandbox"):
        return {"error": "自动化需要先开启「Agent」并设置沙箱目录"}
    live = next((g for g in list_goals()
                 if g.get("session_id") == sid
                 and g.get("state") in ("running", "retrying")), None)
    if live:
        return {"error": "这个会话已经有一个目标在跑，先暂停或停止它"}

    cfg = load_config() or {}
    norm_criteria = _norm_criteria(criteria)
    definition = _definition_from_criteria(norm_criteria) if norm_criteria else {}
    goal = {
        "id": _new_id(),
        "session_id": sid,
        "objective": text,
        "constraints": [],
        "sandbox": agent.clean_sandbox(s.get("sandbox")),
        "criteria": norm_criteria,
        "definition": definition,
        "plan": [],
        "state": "running",
        "active": True,
        "pause_reason": "",
        "turn": 0,
        "max_turns": _int_or(max_turns, cfg.get("autopilot_max_turns"), MAX_TURNS),
        "max_minutes": _int_or(max_minutes, cfg.get("autopilot_max_minutes"), MAX_MINUTES),
        "use_budget": bool(cfg.get("autopilot_use_budget", False)),
        "watchdog_turns": _watchdog_value(cfg.get("autopilot_watchdog_turns")),
        "stall_minutes": _int_zero(cfg.get("autopilot_stall_minutes"), 60),
        "retry_max": _int_zero(cfg.get("autopilot_retry_max"), 0),
        "verify": bool(cfg.get("autopilot_verify", True)),
        "question_policy": normalize_mode(question_policy or cfg.get("autonomy_question_policy")),
        "pending": "",
        "ledger": [],
        "verdict": [],
        "decisions": [],
        "open_questions": [],
        "checkpoint": {},
        "no_progress": 0,
        "progress_sig": "",
        "unknown_streak": 0,
        "bootstrap_tries": 0,
        "bootstrapped": False,
        "retries": 0,
        "last_reason": "",
        "last_error": "",
        "last_text": "",
        "tokens": {"prompt": 0, "completion": 0},
        "schema": 2,
        "created_at": _now(),
        "updated_at": _now(),
        "started_at": _now(),
        "last_progress_at": _now(),
        "finished_at": 0,
    }
    with _LOCK:
        data = load_goals()
        # 新目标接管这个会话：旧的 active 目标（paused / needs_human / failed）
        # 转成历史，避免下次切回来又显示「已暂停」或恢复旧输入。
        for old in data["items"]:
            if old.get("session_id") == sid and _goal_active(old):
                old["active"] = False
                old["last_reason"] = old.get("last_reason") or "已被新目标取代"
                old["updated_at"] = _now()
        data["items"].append(goal)
        # 只留最近 50 个目标，避免文件无限增长
        if len(data["items"]) > 50:
            data["items"] = data["items"][-50:]
        save_goals(data)
    _publish(goal)
    start(goal["id"])
    return goal


def _int_or(v, cfg_v, default):
    for x in (v, cfg_v, default):
        try:
            n = int(x)
            if n > 0:
                return min(n, 1000)
        except (TypeError, ValueError):
            continue
    return default


def _int_zero(*values):
    """允许 0 的整数配置：0 = 关闭 / 不限，缺省 / 非法值用最后一个默认值。"""
    default = values[-1] if values else 0
    for x in values[:-1]:
        try:
            return max(0, min(1000, int(x)))
        except (TypeError, ValueError):
            continue
    return default


def _watchdog_value(v):
    """0 / 非法值 = 关闭看门狗（用一个大数表示）。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_NO_PROGRESS
    if n <= 0:
        return 999999
    return max(2, min(50, n))


def _norm_criteria(raw):
    """验收标准统一成 [{text, cmd, source}]。

    cmd 为空表示「不可自动验收」—— 这种条目裁判只能标 unknown，不能算通过。
    """
    out = []
    if isinstance(raw, list):
        for it in raw[:5]:
            if isinstance(it, str):
                out.append({"text": it.strip(), "cmd": "", "source": "user"})
            elif isinstance(it, dict):
                out.append({
                    "text": str(it.get("text") or "").strip(),
                    "cmd": str(it.get("cmd") or "").strip(),
                    "source": str(it.get("source") or "user"),
                })
    return [c for c in out if c["text"] or c["cmd"]]


def start(gid):
    """起一个驱动线程（重复调用只生效一次）。"""
    with _LOCK:
        th = _THREADS.get(gid)
        if th and th.is_alive():
            return False
        ev = threading.Event()
        _STOP[gid] = ev
        th = threading.Thread(target=_driver, args=(gid, ev), daemon=True,
                              name="autopilot-" + gid)
        _THREADS[gid] = th
        _STARTED[gid] = _now()
        th.start()
    return True


def running_count():
    """还活着的自动化驱动线程数（关窗保活用，见 main._background_job_count）。

    只数线程、不看 goals.json 里的 status：进程重启后残留的
    `status=running` 不一定有线程，拿它当保活依据会让进程永远退不出去。
    """
    with _LOCK:
        return sum(1 for th in _THREADS.values() if th and th.is_alive())


def _stop_thread(gid, join_timeout=0):
    ev = _STOP.get(gid)
    if ev:
        ev.set()
    th = _THREADS.get(gid)
    if (join_timeout and th and th.is_alive()
            and th is not threading.current_thread()):
        th.join(timeout=join_timeout)


def pause(gid, reason="已暂停"):
    _stop_thread(gid)
    g = _update(gid, state="paused", active=True, pause_reason="user",
                last_reason=reason)
    from app.store import ACTIVE_STREAMS
    live = ACTIVE_STREAMS.get((g or {}).get("session_id") or "")
    if live:
        try:
            live.set()
        except Exception:
            pass
    _publish(g)
    return g


def resume(gid):
    g = get_goal(gid)
    if not g:
        return None
    if g.get("state") not in ("paused", "stopped", "failed",
                              "needs_human", "interrupted", "retrying"):
        return g
    # 顺序很重要：先让旧线程退出，再把状态改回 running。
    # 反过来会让旧线程看到 running 又继续跑，和新线程并发写同一个会话。
    _stop_thread(gid, join_timeout=5)
    _g0 = get_goal(gid) or {}
    g = _update(gid, state="running", active=True, pause_reason="",
                last_reason="已恢复", retries=0, last_error="", finished_at=0,
                last_progress_at=_now(),
                sandbox=_resolve_sandbox(_g0),
                checkpoint=_capture_pending_tools(_g0))
    _publish(g)
    start(gid)
    return g


def stop(gid):
    _stop_thread(gid)
    g = _update(gid, state="stopped", active=False, pause_reason="user",
                last_reason="已停止", finished_at=_now())
    from app.store import ACTIVE_STREAMS
    live = ACTIVE_STREAMS.get((g or {}).get("session_id") or "")
    if live:
        try:
            live.set()
        except Exception:
            pass
    _publish(g)
    return g


def status(sid):
    g = goal_for_session(sid)
    if not g:
        return {"ok": True, "goal": None}
    return {"ok": True, "goal": g}


# ------------------------------------------------------------
# 提问策略回调（由 chat.py 调用）
# ------------------------------------------------------------
def mark_needs_human(gid, reason, questions=None):
    """自动化遇到需要用户拍板的问题：暂停目标并记下问题。"""
    if not gid:
        return None
    g = get_goal(gid)
    if not g:
        return None
    qs = [str(q)[:400] for q in (questions or []) if str(q).strip()]
    open_qs = list(g.get("open_questions") or [])
    for q in qs:
        if q not in open_qs:
            open_qs.append(q)
    g = _update(gid, state="needs_human", pause_reason="question",
                last_reason=str(reason or "需要你决定")[:300],
                open_questions=open_qs[-20:])
    _publish(g)
    return g


def record_decision(gid, question, answer, why="", risk="low"):
    """记录一次「系统代答」，方便复盘为什么它自己选了这一项。"""
    if not gid:
        return None
    g = get_goal(gid)
    if not g:
        return None
    rows = list(g.get("decisions") or [])
    rows.append({"q": str(question or "")[:300], "a": str(answer or "")[:300],
                 "why": str(why or "")[:200], "risk": str(risk or "low"),
                 "at": _now()})
    return _update(gid, decisions=rows[-40:])


# ------------------------------------------------------------
# 进程内驱动 _api_chat
# ------------------------------------------------------------
class _FakeHandler:
    """把 SSE 写进内存的假 handler：只为让 _api_chat 能被进程内调用。"""

    def __init__(self, body, on_frame=None):
        self._b = body
        self.events = []
        self.status = 0
        self.payload = None
        self.wfile = self
        self._buf = ""
        self._on_frame = on_frame      # 实时看板钩子：每解析出一帧就回调一次

    def _body(self):
        return self._b

    def _json(self, obj, code=200):
        self.status = code
        self.payload = obj
        return obj

    def send_response(self, code):
        self.status = code

    def send_header(self, _k, _v):
        pass

    def end_headers(self):
        pass

    def write(self, raw):
        try:
            self._buf += raw.decode("utf-8", "replace")
        except Exception:
            return
        while "\n\n" in self._buf:
            frame, self._buf = self._buf.split("\n\n", 1)
            line = next((x for x in frame.split("\n") if x.startswith("data: ")), "")
            if not line:
                continue
            try:
                obj = json.loads(line[6:])
            except ValueError:
                continue
            self.events.append(obj)
            if self._on_frame:
                try:
                    self._on_frame(obj)
                except Exception:
                    pass

    def flush(self):
        pass


def _run_turn(g, instruction, ev):
    """跑一个回合（一次完整对话请求）。返回本轮证据摘要。"""
    from app.api import chat as chat_api

    _qp = g.get("question_policy")
    if not _qp:
        _qp = (load_config() or {}).get("autonomy_question_policy")
    body = {
        "session_id": g["session_id"],
        "content": instruction,
        "autopilot": True,
        "goal_id": g["id"],
        "goal": _goal_brief(g),
        "question_policy": normalize_mode(_qp),
    }
    last = None
    # 实时看板：把回合内的 SSE 帧转成 autopilot_live。delta 是逐字推送的高频帧，
    # 这里按 300ms 合并一批再推，否则一次回合就是上万条事件。
    delta_buf = []
    delta_at = [0.0]

    def flush_delta(force=False):
        if not delta_buf:
            return
        if not force and time.monotonic() - delta_at[0] < 0.3:
            return
        delta_at[0] = time.monotonic()
        chunk = "".join(delta_buf)[-500:]
        del delta_buf[:]
        _push_live(g, "delta", text=chunk)

    def on_frame(frame):
        t = frame.get("type")
        if t == "delta":
            delta_buf.append(str(frame.get("text") or ""))
            if len(delta_buf) > 40:
                del delta_buf[:-40]
                flush_delta(True)
            else:
                flush_delta()
        elif t == "replace":
            del delta_buf[:]
            _push_live(g, "replace", text=str(frame.get("text") or "")[-500:])
        elif t == "tool":
            flush_delta(True)
            _push_live(g, "tool", name=str(frame.get("name") or ""),
                       args=_slim_args(frame.get("args")))
        elif t == "tool_done":
            _push_live(g, "tool_done", name=str(frame.get("name") or ""),
                       ok=frame.get("ok") is not False,
                       ms=int(frame.get("ms") or 0))
        elif t == "step":
            _push_live(g, "step", n=int(frame.get("n") or 0))
        elif t == "warn":
            _push_live(g, "warn", message=str(frame.get("message") or "")[:240])

    for _ in range(8):
        if ev.is_set():
            return {"ok": False, "error": "stopped", "text": "", "tools": 0,
                    "fails": 0, "writes": 0, "warns": [], "timing": {},
                    "usage": {}, "end_reason": {}, "files": [], "errors": []}
        flush_delta(True)       # 上一轮 busy 留下的半截正文不要混进这一轮
        h = _FakeHandler(body, on_frame)
        try:
            chat_api._api_chat(h)
        except Exception as e:
            return {"ok": False, "error": "请求异常：%s" % e, "text": "", "tools": 0,
                    "fails": 0, "writes": 0, "warns": [], "timing": {},
                    "usage": {}, "end_reason": {}, "files": [], "errors": []}
        last = h
        p = h.payload
        if isinstance(p, dict) and p.get("error") == "busy":
            time.sleep(3)              # 用户刚好在手动发消息：等它结束再续
            continue
        break
    if last is None:
        return {"ok": False, "error": "no-try", "text": "", "tools": 0,
                "fails": 0, "writes": 0, "warns": [], "timing": {},
                "usage": {}, "end_reason": {}, "files": [], "errors": []}
    p = last.payload
    if isinstance(p, dict) and p.get("error"):
        return {"ok": False, "error": str(p.get("message") or p.get("error")),
                "text": "", "tools": 0, "fails": 0, "writes": 0, "warns": [],
                "timing": {}, "usage": {}, "end_reason": {}, "files": [],
                "errors": []}
    out = _summarize(last.events)
    out["ok"] = True
    return out


_WRITE_TOOL_NAMES = frozenset(getattr(agent, "WRITE_TOOLS", ()) + getattr(agent, "GIT_WRITE_TOOLS", ()))


def _summarize(evs):
    parts, tools, fails, writes, warns = [], 0, 0, 0, []
    timing, usage, stopped = {}, {}, False
    end_reason, files, errors = {}, [], []
    for e in evs:
        t = e.get("type")
        if t == "delta":
            parts.append(e.get("text") or "")
        elif t == "replace":
            parts = [e.get("text") or ""]
        elif t == "tool_done":
            tools += 1
            if not e.get("ok"):
                fails += 1
            if e.get("write"):
                writes += 1
        elif t == "tool":
            name = str(e.get("name") or "")
            args = e.get("args") if isinstance(e.get("args"), dict) else {}
            if name in _WRITE_TOOL_NAMES:
                p = str(args.get("to") or args.get("path") or "").strip()
                if p and p not in files:
                    files.append(p)
        elif t == "warn":
            warns.append(str(e.get("message") or "")[:200])
        elif t == "error":
            errors.append(str(e.get("message") or "")[:300])
        elif t == "timing":
            timing = e.get("timing") or {}
        elif t == "usage":
            usage = e.get("usage") or {}
        elif t == "end_reason":
            end_reason = e.get("reason") or {}
        elif t == "done":
            stopped = bool(e.get("stopped"))
    text = "".join(parts).strip()
    return {"text": text[-TURN_TEXT_LIMIT:], "tools": tools, "fails": fails,
            "writes": writes, "warns": warns[-3:], "timing": timing,
            "usage": usage, "stopped": stopped, "end_reason": end_reason,
            "files": files[-12:], "errors": errors[-3:]}


# ------------------------------------------------------------
# 立项：完成定义 + 计划
# ------------------------------------------------------------
def _quick(prompt, max_tokens, source):
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return ""
    try:
        return llm._quick_chat(api_key, api_url, api_model,
                               [{"role": "user", "content": prompt}],
                               max_tokens=max_tokens, source=source,
                               timeout=(10, 120)) or ""
    except Exception as e:
        print("[自动化] %s 调用失败：%s" % (source, e))
        return ""


def _bootstrap(g):
    """立项：生成可机检 + 可评审的完成定义，以及一份粗计划。"""
    criteria = g.get("criteria") or []
    if criteria:
        definition = _definition_from_criteria(criteria)
        plan = _bootstrap_plan(g) if not (g.get("plan") or []) else []
        _update(g["id"], bootstrapped=True, definition=definition,
                criteria=_criteria_from_definition(definition),
                plan=plan or (g.get("plan") or []), bootstrap_tries=0)
        return
    tries = int(g.get("bootstrap_tries") or 0) + 1
    prompt = "\n".join([
        "你是项目的验收设计者。把用户目标变成可执行的完成定义。只输出 JSON：",
        '{"deliverables":[{"name":"","path":"","kind":"file|dir|url|app"}],',
        ' "checks":[{"text":"","cmd":"","kind":"command|file|http","path":"","url":""}],',
        ' "rubric":[{"text":"","evidence":"需要什么证据"}],',
        ' "plan":["步骤"]}',
        "",
        "目标：" + (g.get("objective") or ""),
        "",
        "要求：",
        "1. deliverables 3–8 条，写清交付物名称与路径 / 入口；路径不确定可留空。",
        "2. checks 优先给能自动运行的检查（构建 / 测试 / lint / 启动检查 / HTTP 探活），"
        "每条给出完整命令与期望退出码；命令必须能在沙箱根目录直接执行、不需要交互、不依赖外网。",
        "3. 无法自动检查的放 rubric，每条写清「需要什么证据」（文件、运行输出、截图、交互结果）。",
        "4. plan 3–10 步，每步一个可验证动作。",
        "5. 不要假设领域；只根据目标与当前仓库推导。",
        "6. 不要把「让用户确认」写进完成定义；标准要能自己检查。",
    ])
    out = _quick(prompt, BOOTSTRAP_MAX_TOKENS, "bootstrap")
    data = _json_loose(out)
    if not data:
        if tries < 2:
            # 第一次失败先重试一次：多半是上游抖动，不要立刻退回空定义
            _update(g["id"], bootstrap_tries=tries)
            return
        # 两次都拿不到结构化定义：退回「整体达成」的评审项，仍然不允许自称完成
        definition = _empty_definition()
        _update(g["id"], bootstrapped=True, definition=definition,
                criteria=_criteria_from_definition(definition),
                bootstrap_tries=tries, plan=(g.get("plan") or []))
        return
    definition = _norm_definition(data)
    definition = _review_definition(g, definition)
    plan = _plan_from_raw(data.get("plan"))
    _update(g["id"], bootstrapped=True, definition=definition,
            criteria=_criteria_from_definition(definition),
            plan=plan, bootstrap_tries=0)


def _review_definition(g, definition):
    """立项后补漏：从「看起来完成、实际没达标」的角度补 1–4 条 rubric。

    单次立项容易漏掉关键验收（尤其是「尽可能还原」这类模糊目标）；
    这里再让模型做一次验收审查，只补评审项，不重复已有的机检命令。
    """
    existing = "；".join(str(r.get("text") or "")
                        for r in (definition.get("rubric") or []))
    prompt = "\n".join([
        "你是验收审查员。检查下面的「完成定义」是否足以证明目标真的完成。",
        "找出「看起来完成、实际没达标」的漏洞，补充 1–4 条评审项（rubric）。",
        "只输出 JSON：{\"rubric\":[{\"text\":\"验收项\",\"evidence\":\"需要什么证据\"}]}",
        "",
        "## 目标",
        g.get("objective") or "",
        "",
        "## 当前交付物",
        "\n".join("- " + str(d.get("name") or d.get("path") or "")
                  for d in (definition.get("deliverables") or [])[:10]) or "（无）",
        "",
        "## 当前机检",
        "\n".join("- " + str(c.get("text") or c.get("cmd") or "")
                  for c in (definition.get("checks") or [])[:10]) or "（无）",
        "",
        "## 当前评审项",
        existing or "（无）",
        "",
        "要求：不要重复已有条目；不要假设领域；每条的 evidence 必须是可以实际拿到的"
        "（文件、命令输出、截图、交互结果）。",
    ])
    data = _json_loose(_quick(prompt, REVIEW_MAX_TOKENS, "bootstrap"))
    items = data.get("rubric") if isinstance(data.get("rubric"), list) else []
    for it in items[:4]:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text or text in existing:
            continue
        definition.setdefault("rubric", []).append({
            "id": "r%d" % (len(definition.get("rubric") or []) + 1),
            "text": text[:400],
            "evidence": str(it.get("evidence") or
                            "文件、运行输出、截图或可复现的验证步骤").strip()[:400],
            "source": "review",
        })
        existing = (existing + "；" + text) if existing else text
        if len(definition.get("rubric") or []) >= 14:
            break
    return definition


def _bootstrap_plan(g):
    prompt = "\n".join([
        "把下面目标拆成 3–10 步可执行计划，只输出 JSON：",
        '{"plan":["步骤"]}',
        "",
        "目标：" + (g.get("objective") or ""),
        "",
        "验收标准：",
        "\n".join("- " + (c.get("text") or c.get("cmd") or "")
                  for c in (g.get("criteria") or [])[:8]) or "（无）",
        "",
        "要求：每步写清改哪里 / 验证什么；不要写「继续推进」这类空话。",
    ])
    return _plan_from_raw(_json_loose(_quick(prompt, 800, "bootstrap")).get("plan"))


def _plan_from_raw(raw):
    out = []
    if isinstance(raw, list):
        for it in raw[:12]:
            if isinstance(it, str):
                t = it.strip()
            elif isinstance(it, dict):
                t = str(it.get("text") or it.get("title") or "").strip()
            else:
                t = ""
            if t:
                out.append(t[:160])
    return out


# ------------------------------------------------------------
# 裁判：机检 + 独立评审员
# ------------------------------------------------------------
def _capture_pending_tools(g):
    """把「有 intent、没 outcome」的有副作用工具记进断点，续跑时先核实。"""
    try:
        from app import tool_journal
        items = tool_journal.pending(g.get("session_id") or "")
    except Exception:
        items = []
    ck = dict(g.get("checkpoint") or {})
    ck["pending_tools"] = [
        "%s（%s）" % (it.get("tool") or "工具", it.get("call_id") or "?")
        for it in items[:8]
    ]
    return ck


def _resolve_sandbox(g):
    """从会话解析沙箱路径（只在目标没有缓存时调用）。"""
    sid = str(g.get("session_id") or "").strip()
    if not sid:
        return ""
    from app.sessions import find_session, load_sessions
    s = find_session(load_sessions(), sid)
    return agent.clean_sandbox((s or {}).get("sandbox"))


def _sandbox(g):
    """优先用目标里缓存的沙箱：避免每回合都读一遍 20MB+ 的 sessions.json。"""
    cached = agent.clean_sandbox((g or {}).get("sandbox"))
    if cached and os.path.isdir(cached):
        return cached
    return _resolve_sandbox(g or {})


def _ok_text(ok):
    if ok is True:
        return "通过"
    if ok is False:
        return "未通过"
    return "无法判定"


def _verdict_summary(verdict):
    total = len(verdict or [])
    ok = sum(1 for v in verdict or [] if v.get("ok") is True)
    fail = sum(1 for v in verdict or [] if v.get("ok") is False)
    return {"ok": ok, "total": total, "fail": fail,
            "unknown": max(0, total - ok - fail)}


def _file_ok(path, root, require_nonempty=True):
    p = str(path or "").strip()
    if not p:
        return None, "未给出路径"
    full = p if os.path.isabs(p) else os.path.join(root or "", p)
    try:
        if not os.path.exists(full):
            return False, "文件不存在：%s" % full
        if os.path.isdir(full):
            return True, "目录存在：%s" % full
        if require_nonempty and os.path.getsize(full) <= 0:
            return False, "文件为空：%s" % full
        return True, "文件存在，%d 字节" % os.path.getsize(full)
    except OSError as e:
        return False, "检查失败：%s" % e


_CHECK_DANGER_RE = re.compile(
    r"rm\s+-rf|del\s+/[sq]|format\s+[a-z]:|drop\s+table|truncate\s+table|"
    r"git\s+(?:reset\s+--hard|clean\s+-[a-z]*f)",
    re.I)


def _machine_verdict(g, root):
    """跑机检项与交付物存在性检查；返回统一的 verdict 列表。"""
    d = g.get("definition") or {}
    out = []
    for it in d.get("deliverables") or []:
        if not it.get("path"):
            continue                    # 没给路径的交付物已转成 rubric，交给评审员
        p = str(it.get("path") or "").strip()
        if str(it.get("kind") or "").lower() == "url" or p.startswith(("http://", "https://")):
            try:
                with urllib.request.urlopen(p, timeout=8) as resp:
                    code = int(getattr(resp, "status", 0) or 0)
                out.append({"id": it.get("id") or "d?", "text": it.get("name") or "交付物",
                            "kind": "deliverable", "ok": code == 200,
                            "out": "HTTP %d" % code})
            except (urllib.error.URLError, ValueError, OSError) as e:
                out.append({"id": it.get("id") or "d?", "text": it.get("name") or "交付物",
                            "kind": "deliverable", "ok": False,
                            "out": "请求失败：%s" % e})
            continue
        ok, why = _file_ok(p, root)
        out.append({"id": it.get("id") or "d?", "text": it.get("name") or "交付物",
                    "kind": "deliverable", "ok": ok, "out": why})
    for it in d.get("checks") or []:
        kind = str(it.get("kind") or "command")
        text = it.get("text") or it.get("cmd") or ""
        if kind == "command":
            cmd = str(it.get("cmd") or "").strip()
            if not cmd:
                out.append({"id": it.get("id") or "c?", "text": text,
                            "kind": "check", "ok": None, "out": "缺少命令"})
                continue
            if not root or not os.path.isdir(root):
                out.append({"id": it.get("id") or "c?", "text": text,
                            "kind": "check", "ok": None,
                            "out": "未设置沙箱，无法执行验收命令"})
                continue
            # 验收命令与员工走同一条「完全放权」策略：员工本来就能跑这些命令，
            # 裁判不跑反而会让「npm i && npm run build」永远无法通过。
            # 只额外拦住明显的破坏性命令（沙箱边界仍由执行入口把关）。
            if _CHECK_DANGER_RE.search(cmd):
                out.append({"id": it.get("id") or "c?", "text": text,
                            "kind": "check", "ok": None,
                            "out": "验收命令包含破坏性操作，未自动执行"})
                continue
            try:
                decision, why = agent.judge_command(
                    root, {"command": cmd, "timeout": CRITERIA_CMD_TIMEOUT},
                    "full", "standard", interactive=False)
            except Exception as e:
                decision, why = "deny", "命令校验失败：%s" % e
            if decision != "allow":
                out.append({"id": it.get("id") or "c?", "text": text,
                            "kind": "check", "ok": None,
                            "out": "验收命令未自动执行（%s）：%s" % (decision, why)})
                continue
            try:
                t, _undo, _w = agent.run_tool(
                    root, "run_command",
                    {"command": cmd, "timeout": CRITERIA_CMD_TIMEOUT},
                    unrestricted=True)
            except Exception as e:
                t = "❌ 验收命令执行失败：%s" % e
            ok = ("退出码：0" in t) and not str(t).lstrip().startswith("❌")
            out.append({"id": it.get("id") or "c?", "text": text, "kind": "check",
                        "ok": ok, "out": str(t)[-1200:]})
        elif kind == "file":
            ok, why = _file_ok(it.get("path"), root)
            out.append({"id": it.get("id") or "c?", "text": text,
                        "kind": "check", "ok": ok, "out": why})
        elif kind == "http":
            url = str(it.get("url") or "").strip()
            if not url:
                out.append({"id": it.get("id") or "c?", "text": text,
                            "kind": "check", "ok": None, "out": "缺少 URL"})
                continue
            try:
                with urllib.request.urlopen(url, timeout=8) as resp:
                    code = int(getattr(resp, "status", 0) or 0)
                out.append({"id": it.get("id") or "c?", "text": text,
                            "kind": "check", "ok": code == 200,
                            "out": "HTTP %d" % code})
            except (urllib.error.URLError, ValueError, OSError) as e:
                out.append({"id": it.get("id") or "c?", "text": text,
                            "kind": "check", "ok": False, "out": "请求失败：%s" % e})
        else:
            out.append({"id": it.get("id") or "c?", "text": text,
                        "kind": "check", "ok": None, "out": "未知检查类型：%s" % kind})
    return out


def _ledger_lines(g):
    rows = (g.get("ledger") or [])[-LEDGER_KEEP:]
    out = []
    for r in rows:
        bits = ["第 %s 轮" % r.get("turn"),
                "工具 %s 次" % r.get("tools", 0),
                "失败 %s 次" % r.get("fails", 0),
                "写入 %s 次" % r.get("writes", 0)]
        if r.get("checks_total"):
            bits.append("验收 %s/%s" % (r.get("checks_ok", 0), r.get("checks_total")))
        if r.get("finish"):
            bits.append("收尾=%s" % r.get("finish"))
        if r.get("warns"):
            bits.append("告警=" + " / ".join(r.get("warns")[:2]))
        out.append("· " + "，".join(bits))
    return out or ["（还没有执行记录）"]


def _workspace_evidence(root):
    if not root or not os.path.isdir(root):
        return "（未设置沙箱）"
    lines = []
    for cmd in ("git status --short", "git diff --stat"):
        try:
            t, _u, _w = agent.run_tool(root, "run_command",
                                       {"command": cmd, "timeout": 30})
            t = str(t or "").strip()
            if "退出码：0" in t:
                body = t.split("输出：", 1)[-1].strip()
                _lines = body.splitlines()
                if len(_lines) > 60:
                    body = ("\n".join(_lines[:60])
                            + "\n…（共 %d 行，已截断）" % len(_lines))
                lines.append("$ %s\n%s" % (cmd, body[-1600:] or "（空）"))
        except Exception:
            pass
    return "\n".join(lines) or "（不是 git 仓库，或没有改动可看）"


def _ask_verifier(g, root, machine):
    """独立评审员：只看目标 + 完成定义 + 原始证据，不看员工自述。"""
    rubric = (g.get("definition") or {}).get("rubric") or []
    if not rubric:
        return {}
    evidence = []
    for v in machine:
        evidence.append("- [%s] %s → %s｜%s" % (
            v.get("kind"), v.get("text"), _ok_text(v.get("ok")),
            (v.get("out") or "").replace("\n", " ")[:600]))
    evidence.append("## 工作区证据\n" + _workspace_evidence(root))
    evidence.append("## 最近执行记录\n" + "\n".join(_ledger_lines(g)))
    prompt = "\n".join([
        "你是独立验收员。你只相信证据，不相信执行者的自述。",
        "",
        "## 目标",
        g.get("objective") or "",
        "",
        "## 完成定义（只判 rubric）",
        "\n".join("- [%s] %s｜需要证据：%s" % (
            r.get("id"), r.get("text"), r.get("evidence") or "（未写）")
            for r in rubric),
        "",
        "## 证据",
        "\n".join(evidence)[:12000],
        "",
        "## 输出（严格 JSON）",
        '{"items":[{"id":"r1","ok":true|false|null,"why":"依据","need":"还缺什么证据"}]}',
        "",
        "规则：",
        "1. ok=true 仅当证据足以证明该条满足；",
        "2. ok=false 当证据显示不满足；",
        "3. ok=null 当证据不足，不要猜；",
        "4. 不要因为执行者说「已完成」就判通过。",
    ])
    out = _quick(prompt, VERIFY_MAX_TOKENS, "verifier")
    data = _json_loose(out)
    items = data.get("items") if isinstance(data.get("items"), list) else []
    got = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        rid = str(it.get("id") or "").strip()
        if not rid:
            continue
        raw_ok = it.get("ok")
        if isinstance(raw_ok, str):
            s = raw_ok.strip().lower()
            ok = True if s in ("true", "pass", "passed", "yes", "1") else (
                False if s in ("false", "fail", "failed", "no", "0") else None)
        elif raw_ok is True or raw_ok is False:
            ok = raw_ok
        else:
            ok = None
        got[rid] = {"ok": ok,
                    "out": "%s%s" % (str(it.get("why") or "").strip()[:500],
                                     ("；还缺：" + str(it.get("need") or "").strip()[:300])
                                     if str(it.get("need") or "").strip() else "")}
    return got


def _verify_definition(g, cfg=None):
    """返回统一 verdict：每条 {id,text,kind,ok,out}；ok=None 表示无法自动验收。"""
    cfg = cfg or (load_config() or {})
    root = _sandbox(g)
    machine = _machine_verdict(g, root)
    rubric = (g.get("definition") or {}).get("rubric") or []
    if not rubric:
        return machine
    if not bool(g.get("verify", cfg.get("autopilot_verify", True))):
        for r in rubric:
            machine.append({"id": r.get("id"), "text": r.get("text"),
                            "kind": "rubric", "ok": None,
                            "out": "独立评审员已关闭，无法自动判定"})
        return machine
    got = {}
    if rubric:
        _phase(g, "review", "独立评审员正在核对证据…")
        got = _ask_verifier(g, root, machine)
    for r in rubric:
        rid = str(r.get("id") or "")
        hit = got.get(rid)
        if hit:
            machine.append({"id": rid, "text": r.get("text"), "kind": "rubric",
                            "ok": hit.get("ok"), "out": hit.get("out") or ""})
        else:
            machine.append({"id": rid, "text": r.get("text"), "kind": "rubric",
                            "ok": None, "out": "评审员没有给出结论"})
    return machine


def _all_pass(verdict):
    """全部通过才算完成；ok=None（不可自动验收）不算通过，避免「假装完成了」。"""
    if not verdict:
        return False
    return all(v.get("ok") is True for v in verdict)


def _gaps_text(verdict):
    bad = [v for v in (verdict or []) if v.get("ok") is not True]
    if not bad:
        return ""
    lines = ["验收未通过（这是外部检查 / 评审员的真实结果，不是你的自述）："]
    for v in bad[:6]:
        out = str(v.get("out") or "").replace("\n", " ").strip()[:300]
        lines.append("- [%s] %s：%s" % (v.get("kind") or "check",
                                        v.get("text") or v.get("id") or "",
                                        out or "未通过"))
    if len(bad) > 6:
        lines.append("- 另有 %d 条未通过。" % (len(bad) - 6))
    lines.append("请针对这些缺口继续修，不要宣布完成。")
    return "\n".join(lines)


# ------------------------------------------------------------
# 老板
# ------------------------------------------------------------
_BOSS_SYS = (
    "你是这个项目的「老板」，只负责决策，不负责执行。\n"
    "你要根据证据判断员工下一步做什么，而不是复述员工的话。\n"
    "只输出一个 JSON 对象，不要解释：")


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


def _verdict_lines(verdict):
    lines = []
    for v in (verdict or [])[:10]:
        lines.append("- [%s] %s：%s%s" % (
            v.get("kind") or "check", v.get("text") or v.get("id") or "",
            _ok_text(v.get("ok")),
            ("｜" + str(v.get("out") or "")[-240:].replace("\n", " "))
            if v.get("out") else ""))
    return lines or ["（还没有验收结论）"]


def _boss_ask(g, verdict, turn):
    """让老板做一次决策。失败返回 None（调用方按「继续」处理）。"""
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return None
    crit = g.get("criteria") or []
    dlines = _definition_lines(g.get("definition") or {})
    crit_txt = "（见完成定义）" if dlines else ("\n".join(
        "- [%s] %s%s" % (c.get("source") or "user", c.get("text") or "",
                         ("｜命令：" + c["cmd"]) if c.get("cmd") else "｜不可自动验收")
        for c in crit) or "（无）")
    prompt = "\n".join([
        "## 目标",
        g.get("objective") or "",
        "",
        "## 用户约束",
        ("\n".join("- " + c for c in (g.get("constraints") or [])) or "（无）"),
        "",
        "## 完成定义",
        "\n".join(dlines) or "（无）",
        "",
        "## 旧验收标准（兼容）",
        crit_txt,
        "",
        "## 裁判结论（外部检查 / 评审员的真实结果，优先于员工的任何说法）",
        "\n".join(_verdict_lines(verdict)),
        "",
        "## 最近几轮证据",
        "\n".join(_ledger_lines(g)),
        "",
        "## 员工最后一段话（可能不准确，只作参考）",
        (g.get("last_text") or "（无）"),
        "",
        "## 进度",
        "已用 %s，%s 分钟；连续无进展 %s 轮。" % (
            ("%s/%s 回合" % (turn, g.get("max_turns")))
            if _use_budget(g) else ("%s 回合（不限）" % turn),
            _minutes(g), int(g.get("no_progress") or 0)),
        "",
        "## 输出（严格 JSON）",
        '{"decision": "continue|replan|propose_done|ask_human", '
        '"instruction": "给员工的下一句可执行指令", '
        '"plan_patch": ["..."], "reason": "一句话说明依据"}',
        "",
        "规则：",
        "1. 只有裁判结论**全部通过**才能 propose_done；有未通过 / 无法判定的必须 continue；",
        "2. instruction 必须是可执行的下一步（做什么、改哪里、怎么验证），"
        "不要写「继续加油」「保持进度」这类空话，也不要重复上一轮说过的话；",
        "3. 需要用户拍板（不可逆操作、涉及偏好、缺外部信息）才 ask_human；",
        "4. 验收项标注「无法判定」时不要据此 propose_done，改为 continue 让员工补证据；",
        "5. plan_patch 只在需要调整计划时给，每项是一句新的计划步骤（最多 3 条）；",
        "6. 连续多轮没有实际进展时，必须 replan：把任务拆小或换一种做法。",
    ])
    try:
        out = llm._quick_chat(api_key, api_url, api_model,
                              [{"role": "system", "content": _BOSS_SYS},
                               {"role": "user", "content": prompt}],
                              max_tokens=BOSS_MAX_TOKENS, source="boss",
                              timeout=(10, 90)) or ""
    except Exception as e:
        print("[自动化] 老板调用失败：%s" % e)
        return None
    data = _json_loose(out)
    decision = str(data.get("decision") or "").strip().lower()
    if decision not in ("continue", "replan", "propose_done", "ask_human"):
        decision = "continue"
    instr = str(data.get("instruction") or "").strip()
    if decision in ("continue", "replan") and not instr:
        instr = "按目标继续下一步，并在结束前用工具验证结果。"
    patch = data.get("plan_patch")
    patch = [str(x).strip() for x in patch if str(x).strip()][:3] if isinstance(patch, list) else []
    return {"decision": decision, "instruction": instr[:600],
            "plan_patch": patch, "reason": str(data.get("reason") or "").strip()[:200]}


# ------------------------------------------------------------
# 预算 / 进度 / 错误分类
# ------------------------------------------------------------
def _minutes(g):
    started = float(g.get("started_at") or 0)
    if not started:
        return 0
    return int((_now() - started) / 60)


def _use_budget(g, cfg=None):
    if "use_budget" in g:
        return bool(g.get("use_budget"))
    return bool((cfg or load_config() or {}).get("autopilot_use_budget", False))


def _budget_over(g):
    if int(g.get("turn") or 0) >= int(g.get("max_turns") or MAX_TURNS):
        return "回合数用尽"
    if _minutes(g) >= int(g.get("max_minutes") or MAX_MINUTES):
        return "时长用尽"
    return ""


def _stall_over(g, cfg=None):
    """长时间没有任何进展（包括一直重试）→ 暂停，避免无限烧 token。"""
    raw = g.get("stall_minutes")
    if raw is None:
        raw = (cfg or {}).get("autopilot_stall_minutes", 60)
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        limit = 60
    if limit <= 0:
        return False
    last = float(g.get("last_progress_at") or g.get("started_at") or _now())
    return (_now() - last) >= limit * 60


def _watchdog_turns(g):
    try:
        n = int(g.get("watchdog_turns") or DEFAULT_NO_PROGRESS)
    except (TypeError, ValueError):
        n = DEFAULT_NO_PROGRESS
    return 999999 if n <= 0 else max(2, n)


def _workspace_files_digest(root, files):
    """对本次改过的文件取内容摘要：识别「反复重写同一内容」这种假进展。"""
    h = hashlib.sha1()
    for p in sorted({str(x).strip() for x in (files or []) if str(x).strip()}):
        full = p if os.path.isabs(p) else os.path.join(root or "", p)
        try:
            st = os.stat(full)
            h.update(p.encode("utf-8"))
            h.update(str(st.st_size).encode("utf-8"))
            with open(full, "rb") as f:
                h.update(hashlib.sha1(f.read(2 * 1024 * 1024)).digest())
        except OSError:
            h.update((p + ":missing").encode("utf-8"))
    return h.hexdigest()[:16]


def _progress_signature(g, res):
    files = (res or {}).get("files") or []
    digest = _workspace_files_digest(_sandbox(g), files)
    writes = sum(int(r.get("writes") or 0) for r in (g.get("ledger") or []))
    # 知道改了哪些文件时只看内容摘要：同一内容反复重写不算进展；
    # 不知道文件（删除 / 移动 / 命令类）时才退回累计写入次数。
    # 计划变更不算进展：否则反复 replan 就能骗过看门狗。
    counter = 0 if files else writes
    verdict = tuple((str(v.get("id") or ""), v.get("ok"))
                    for v in (g.get("verdict") or []))
    return "%s|%d|%s" % (digest, counter, verdict)


def _next_no_progress(g, sig):
    prev = str(g.get("progress_sig") or "")
    if prev and prev == sig:
        return int(g.get("no_progress") or 0) + 1
    return 0


_FATAL_RE = re.compile(
    r"401|403|unauthorized|invalid[_ ]?api[_ ]?key|密钥.*(?:错误|无效|不正确)|"
    r"余额|billing|insufficient|quota|额度不足|"
    r"400|bad request|404|not found|模型不存在",
    re.I)
_TRANSIENT_RE = re.compile(
    r"timeout|timed out|超时|连接|connection|reset|eof|network|网络|"
    r"429|rate limit|限流|500|502|503|504|server|上游|"
    r"中断|idle|空回合|没有返回|busy|繁忙",
    re.I)


def _error_kind(msg):
    s = str(msg or "")
    if not s:
        return "other"
    if _FATAL_RE.search(s):
        return "account" if re.search(r"401|403|unauthorized|密钥|余额|billing|"
                                       r"insufficient|quota|额度", s, re.I) else "fatal"
    if _TRANSIENT_RE.search(s):
        return "transient"
    return "other"


def _retry_delay(attempt):
    try:
        n = max(1, int(attempt))
    except (TypeError, ValueError):
        n = 1
    return min(RETRY_CAP, RETRY_BASE * (2 ** (n - 1)))


def _resume_instruction(err):
    return ("上一轮因「%s」中断。先从断点继续：先核对当前文件 / 命令状态，"
            "不要重复已经完成的写入；然后继续当前计划步骤。" % str(err or "上游中断")[:200])


def _stuck_instruction(n):
    return ("【停滞恢复】连续 %d 回合没有新的写入 / 计划变更 / 验收变化。"
            "不要再重复读同一批文件：把剩余工作拆成更小的可验证步骤，换一种实现，"
            "或先做一个最小可运行版本。确实被外部条件卡住，就在正文里说清缺什么。" % n)


def _instruction(g):
    pending = str(g.get("pending") or "").strip()
    if pending:
        return pending
    if int(g.get("turn") or 0) <= 0:
        return str(g.get("objective") or "").strip() or "按目标开始执行。"
    return ("继续按目标和完成定义推进。先核对当前状态与断点，"
            "不要重复已完成的写入；完成后用工具验证。")


def _finish(gid, state, reason, pause_reason=""):
    active = state == "needs_human" or (
        state in ("paused", "failed") and bool(pause_reason))
    g = _update(gid, state=state, active=active,
                last_reason=str(reason or "")[:500],
                pause_reason=pause_reason, finished_at=_now())
    _publish(g)
    _phase(g, "done", reason, state=state)
    return g


# ------------------------------------------------------------
# 驱动
# ------------------------------------------------------------
def _driver(gid, ev):
    try:
        _loop(gid, ev)
    except Exception as e:
        print("[自动化] 驱动异常（%s）：%s" % (gid, e))
        g = _update(gid, state="failed", active=True, last_error=str(e)[:500],
                    last_reason="驱动异常：%s" % str(e)[:200],
                    pause_reason="error", finished_at=_now())
        _publish(g)
    finally:
        # 线程自己退出时把登记清掉，否则 resume 会以为「线程还活着」而不重启
        with _LOCK:
            if _THREADS.get(gid) is threading.current_thread():
                _THREADS.pop(gid, None)


def _loop(gid, ev):
    while True:
        g = get_goal(gid)
        if not g or g.get("state") not in ("running", "retrying") or ev.is_set():
            return
        g = _migrate_goal(g)
        cfg = load_config() or {}
        if not g.get("sandbox"):
            # 旧目标没有缓存沙箱：解析一次并落库，后面每回合直接读
            _update(gid, sandbox=_resolve_sandbox(g))
            g = get_goal(gid) or g

        if _use_budget(g, cfg):
            over = _budget_over(g)
            if over:
                _finish(gid, "paused", over, pause_reason="budget")
                return
        if _stall_over(g, cfg):
            _finish(gid, "needs_human",
                    "长时间没有实际进展，已暂停（可检查网络 / 模型后恢复）",
                    pause_reason="stall")
            return

        # 没有完成定义就立项（旧目标 / 上次立项失败都走这里）
        if not _has_definition(g):
            _phase(g, "plan", "正在拆解目标、生成完成定义…",
                   turn=int(g.get("turn") or 0) + 1)
            _bootstrap(g)
            g = get_goal(gid) or g
            if not g.get("bootstrapped"):
                # 还没拿到完成定义：先别跑员工回合，下一圈重试立项
                continue

        turn = int(g.get("turn") or 0) + 1
        step_text = _instruction(g)
        _phase(g, "run", step_text, turn=turn)
        res = _run_turn(g, step_text, ev)
        if ev.is_set():
            # 用户在回合进行中点过暂停 / 停止：不要再写状态覆盖它
            return
        if not res.get("ok"):
            err = str(res.get("error") or "")
            if err == "stopped" or ev.is_set():
                return
            kind = _error_kind(err)
            if kind in ("transient", "other"):
                retries = int(g.get("retries") or 0) + 1
                _update(gid, state="retrying", retries=retries,
                        last_error=err[:500],
                        last_reason="上游中断，自动重试第 %d 次" % retries)
                _publish(get_goal(gid))
                limit = int(g.get("retry_max") or 0)
                if limit > 0 and retries > limit:
                    _finish(gid, "needs_human",
                            "连续重试 %d 次仍失败：%s" % (retries, err[:300]),
                            pause_reason="error")
                    return
                if ev.wait(_retry_delay(retries)):
                    return
                _update(gid, state="running", pending=_resume_instruction(err),
                        last_reason="重试第 %d 次" % retries)
                _publish(get_goal(gid))
                continue
            # 账户 / 参数类错误：重试没有意义，停下来让人处理
            _finish(gid, "needs_human" if kind == "account" else "failed",
                    "回合执行失败：%s" % err[:300], pause_reason="error")
            return

        end_reason = res.get("end_reason") or {}
        if end_reason.get("kind") == "stopped":
            # 用户在会话里点了「停止」：别让看门狗马上又拉起来。
            g2 = get_goal(gid)
            if g2 and g2.get("state") in ("running", "retrying"):
                _finish(gid, "paused", "你在会话里停止了本轮",
                        pause_reason="user")
            return
        g2 = get_goal(gid) or g
        if g2.get("state") == "needs_human":
            # 员工在提问卡上停下来了：目标已经标成 needs_human，等用户回答
            return
        if end_reason.get("kind") == "billing":
            _finish(gid, "needs_human",
                    end_reason.get("text") or "API 余额不足",
                    pause_reason="billing")
            return
        if (end_reason.get("kind") == "network"
                and not res.get("tools") and not res.get("writes")):
            # 上游没产出：按瞬态错误处理，不消耗工作回合
            retries = int(g.get("retries") or 0) + 1
            _update(gid, state="retrying", retries=retries,
                    last_error=str(end_reason.get("text") or "上游中断")[:500],
                    last_reason="上游中断，自动重试第 %d 次" % retries)
            _publish(get_goal(gid))
            limit = int(g.get("retry_max") or 0)
            if limit > 0 and retries > limit:
                _finish(gid, "needs_human",
                        "连续重试 %d 次仍没有产出：%s"
                        % (retries, str(end_reason.get("text") or "")[:300]),
                        pause_reason="error")
                return
            if ev.wait(_retry_delay(retries)):
                return
            _update(gid, state="running",
                    pending=_resume_instruction(end_reason.get("text")),
                    last_reason="重试第 %d 次" % retries)
            _publish(get_goal(gid))
            continue

        usage = res.get("usage") or {}
        tok = g.get("tokens") or {}
        tok["prompt"] = int(tok.get("prompt") or 0) + int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        tok["completion"] = int(tok.get("completion") or 0) + int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        timing = res.get("timing") or {}
        entry = {
            "turn": turn,
            "instruction": str(_instruction(g))[:300],
            "tools": res.get("tools") or 0,
            "fails": res.get("fails") or 0,
            "writes": res.get("writes") or 0,
            "finish": timing.get("finish_reason") or "",
            "rounds": timing.get("rounds") or 0,
            "warns": res.get("warns") or [],
            "at": _now(),
        }
        ledger = list(g.get("ledger") or []) + [entry]
        _update(gid, turn=turn, ledger=ledger[-60:], tokens=tok, retries=0,
                last_text=(res.get("text") or "")[-TURN_TEXT_LIMIT:],
                last_error="")
        g = get_goal(gid) or g

        # 裁判：先机检，再（必要时）独立评审。
        # 没有新写入、且最近刚判过时复用上次结论，避免每回合都跑一遍测试 / 评审。
        last_verify = int(g.get("last_verify_turn") or -99)
        if (res.get("writes") or not (g.get("verdict") or [])
                or (turn - last_verify) >= 2):
            _phase(g, "verify", "正在跑机检与验收…", turn=turn)
            verdict = _verify_definition(g, cfg)
            _update(gid, verdict=verdict, last_verify_turn=turn)
            g = get_goal(gid) or g
            _push_live(g, "verdict", turn=turn,
                       ok=sum(1 for v in verdict if v.get("ok") is True),
                       total=len(verdict))
            # 把验收进度补进最近一条 ledger，老板看证据时能直接看到「过了几条」
            _ledger = list(g.get("ledger") or [])
            if _ledger:
                _ledger[-1]["checks_ok"] = sum(1 for v in verdict if v.get("ok") is True)
                _ledger[-1]["checks_total"] = len(verdict)
                _update(gid, ledger=_ledger)
                g = get_goal(gid) or g
        else:
            verdict = g.get("verdict") or []
        if ev.is_set():
            return
        if _all_pass(verdict):
            _finish(gid, "done", "验收通过", pause_reason="")
            return

        sig = _progress_signature(g, res)
        no_progress = _next_no_progress(g, sig)
        fields = {"progress_sig": sig, "no_progress": no_progress}
        if no_progress == 0:
            fields["last_progress_at"] = _now()
        _update(gid, **fields)
        g = get_goal(gid) or g

        limit = _watchdog_turns(g)
        if no_progress >= limit * 2:
            _finish(gid, "needs_human",
                    "连续 %d 回合没有实际进展，已暂停" % no_progress,
                    pause_reason="stuck")
            return

        _phase(g, "boss", "看下一步该怎么走…", turn=turn)
        boss = _boss_ask(g, verdict, turn) or {
            "decision": "continue",
            "instruction": "按目标继续下一步，并在结束前用工具验证结果。",
            "plan_patch": [], "reason": "老板未返回，按继续处理",
        }
        if ev.is_set():
            return
        decision = boss["decision"]
        if no_progress >= limit and decision != "ask_human":
            decision = "replan"
            boss["instruction"] = _stuck_instruction(no_progress)

        plan = list(g.get("plan") or [])
        patch = boss.get("plan_patch") or []
        if patch:
            plan = (plan + patch)[-12:]

        if decision == "propose_done":
            # 老板想收工但裁判不同意：把缺口原样给员工，继续做
            gaps = _gaps_text(verdict) or "验收还未全部通过，请继续补齐缺口。"
            _update(gid, plan=plan, pending=gaps,
                    checkpoint={"turn": turn, "files": res.get("files") or [],
                                "verdict": _verdict_summary(verdict),
                                "pending_tools": [],
                                "pending": gaps[:300]})
            _publish(get_goal(gid))
            continue

        if decision == "ask_human":
            _finish(gid, "needs_human",
                    "需要你决定：" + (boss.get("reason") or ""),
                    pause_reason="human")
            return

        gaps = _gaps_text(verdict)
        pending = "\n\n".join([x for x in (boss.get("instruction"), gaps) if x])
        if not pending:
            pending = "继续下一步，并在结束前用工具验证。"
        _update(gid, plan=plan, pending=pending,
                checkpoint={"turn": turn, "files": res.get("files") or [],
                            "verdict": _verdict_summary(verdict),
                            "pending_tools": [],
                            "pending": pending[:300]})
        _publish(get_goal(gid))


# ------------------------------------------------------------
# 启动恢复 / 看门狗
# ------------------------------------------------------------
def _live(gid):
    th = _THREADS.get(gid)
    return bool(th and th.is_alive())


def _auto_resume_on(cfg):
    """自动恢复开关；自检 / 测试里用环境变量强制关掉，避免误跑真实目标。"""
    env = str(os.environ.get("GALGAME_NO_AUTORESUME") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return False
    return bool((cfg or {}).get("autopilot_resume_on_start", True))


def recover():
    """进程重启后的收敛：未完成的目标接回线程，避免「卡在 running 没人跑」。"""
    env = str(os.environ.get("GALGAME_NO_AUTORESUME") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        # 自检 / 测试进程：完全不要碰真实 goals.json（连状态迁移也不做）
        return 0
    cfg = load_config() or {}
    auto_resume = _auto_resume_on(cfg)
    count = 0
    with _LOCK:
        data = load_goals()
        changed = False
        for g in data["items"]:
            before = json.dumps(g, ensure_ascii=False, sort_keys=True, default=str)
            g = _migrate_goal(g)
            if json.dumps(g, ensure_ascii=False, sort_keys=True, default=str) != before:
                changed = True
            if g.get("state") not in ("running", "retrying", "interrupted"):
                continue
            if auto_resume:
                g["state"] = "running"
                g["active"] = True
                g["pause_reason"] = ""
                g["retries"] = 0
                g["last_reason"] = "应用重启后自动恢复"
                g["last_progress_at"] = _now()   # 关机期间不算「停滞」
                g["checkpoint"] = _capture_pending_tools(g)
                count += 1
            else:
                g["state"] = "paused"
                g["active"] = True
                g["pause_reason"] = "restart"
                g["last_reason"] = "应用已重启；点恢复继续"
            g["updated_at"] = _now()
            changed = True
        if changed:
            save_goals(data)
    if auto_resume and count:
        for g in list_goals():
            if g.get("state") == "running" and not _live(g.get("id")):
                start(g.get("id"))
    return count


def start_watchdog():
    """后台看门狗：线程意外退出 / 重启后没接上的目标，自动重新拉起。"""
    if str(os.environ.get("GALGAME_NO_AUTORESUME") or "").strip().lower() in (
            "1", "true", "yes", "on"):
        return False
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        if _WATCHDOG and _WATCHDOG.is_alive():
            return False
        _WATCHDOG = threading.Thread(target=_watchdog_loop, daemon=True,
                                     name="autopilot-watchdog")
        _WATCHDOG.start()
    return True


def _watchdog_loop():
    while True:
        time.sleep(20)
        try:
            cfg = load_config() or {}
            if not _auto_resume_on(cfg):
                continue
            for g in list_goals():
                if g.get("state") not in ("running", "retrying"):
                    continue
                gid = g.get("id")
                if _live(gid):
                    continue
                if _now() - float(_STARTED.get(gid) or 0) < START_COOLDOWN:
                    continue
                print("[自动化] 看门狗重新拉起目标：%s" % gid)
                start(gid)
        except Exception as e:                 # noqa: BLE001  看门狗不能自己死
            print("[自动化] 看门狗异常：%s" % e)
