# -*- coding: utf-8 -*-
"""AI 改动审阅：把一段 diff 交给模型，产出摘要、风险点与 mermaid 流程图。

两类输入：
· scope=ai  —— 「AI 变更审阅」队列里的文件（按 undo 快照算 diff）；
· scope=git —— 未提交改动 / 单个提交 / 分支差异（由 app.git.diff_text 给出文本）。

模型输出要求是严格 JSON，但小模型经常夹带解释或围栏，所以解析要宽松：
先剥围栏，再截取第一个 `{` 到最后一个 `}`；实在解析不了就把原文当摘要，
不让整个功能因为格式问题失败。
"""
import json
import re

from app import agent, diffview
from app.config import get_api_conf
from app.llm import _quick_chat

MAX_DIFF_CHARS = 60000        # 交给模型的 diff 总长上限
MAX_FILE_DIFF = 12000         # 单文件 diff 上限
REVIEW_MAX_TOKENS = 1600
MESSAGE_MAX_TOKENS = 240


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _hunks_text(d, limit=MAX_FILE_DIFF):
    """把 diffview 的 hunk 结构转成紧凑的 unified 文本（只保留真正增删的行）。"""
    out = []
    for h in d.get("hunks") or []:
        old = h.get("old") or []
        new = h.get("new") or []
        ok = h.get("old_kinds") or []
        nk = h.get("new_kinds") or []
        out.append("@@ -%d +%d @@" % (int(h.get("old_start") or 1),
                                      int(h.get("new_start") or 1)))
        for i, line in enumerate(old):
            if i < len(ok) and ok[i] == "del":
                out.append("- " + line)
        for i, line in enumerate(new):
            if i < len(nk) and nk[i] == "ins":
                out.append("+ " + line)
        if len("\n".join(out)) > limit:
            break
    text = "\n".join(out)
    return text[:limit]


def ai_diff(sb, sid, items):
    """按审阅队列收集 AI 改动，返回 {ok,label,text,files,skipped}。"""
    agent.ensure_undo_loaded(sid)
    files = []
    skipped = 0
    seen = set()
    for it in items or []:
        rel = str((it or {}).get("path") or "").strip().replace("\\", "/")
        token = str((it or {}).get("undo") or "").strip()
        if not rel or not token or rel in seen:
            continue
        seen.add(rel)
        try:
            target = agent.resolve_path(sb, rel)
        except agent.SandboxError:
            skipped += 1
            continue
        old = agent.undo_old_content(token, target)
        if old is None:
            skipped += 1
            continue
        new = _read_text(target)
        d = diffview.build(old, new, 3)
        if not d["adds"] and not d["dels"]:
            continue
        files.append({"path": rel, "adds": d["adds"], "dels": d["dels"],
                      "text": _hunks_text(d)})
    if not files:
        return {"ok": False, "error": "没有可审查的改动（快照可能已过期）",
                "label": "", "text": "", "files": [], "skipped": skipped}
    label = "AI 改动 · %d 个文件" % len(files)
    text = "\n\n".join("### %s (+%d −%d)\n%s" % (f["path"], f["adds"], f["dels"], f["text"])
                       for f in files)
    return {"ok": True, "error": "", "label": label,
            "text": text[:MAX_DIFF_CHARS], "files": files, "skipped": skipped}


def _strip_fence(text):
    t = str(text or "").strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl >= 0 else ""
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return t.strip()


def _json_object(text):
    """从模型输出里抠出 JSON 对象；失败返回 None。"""
    t = _strip_fence(text)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(t[i:j + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _clean_flow(flow):
    """mermaid 源码：去掉围栏和常见的前后噪声。"""
    t = _strip_fence(flow)
    t = re.sub(r"^\s*(?:```)?mermaid\s*", "", t, flags=re.I)
    return t.strip()


def parse_review(raw):
    """把模型输出解析成 {summary, risks, flow}；格式坏了也不报错。"""
    obj = _json_object(raw)
    if obj is None:
        return {"summary": _strip_fence(raw)[:1200], "risks": [], "flow": ""}
    summary = str(obj.get("summary") or "").strip()
    risks = obj.get("risks") or []
    if isinstance(risks, str):
        risks = [risks]
    if not isinstance(risks, list):
        risks = []
    risks = [str(x).strip() for x in risks if str(x or "").strip()]
    return {"summary": summary or "（模型没有返回摘要）",
            "risks": risks[:8],
            "flow": _clean_flow(obj.get("flow") or "")}


def _review_prompt(label, diff_text):
    sys = (
        "你是代码审阅助手。下面是一组改动（- 是删除行，+ 是新增行）。"
        "请用中文给出：\n"
        "1) 一句话总结这次改动的目的和影响范围；\n"
        "2) 3-6 条风险或需要确认的点（确实没有明显风险时写「没有明显风险」）；\n"
        "3) 一张 mermaid flowchart，展示改动涉及的模块与数据流。"
        "节点标签只用中文或英文单词，不要用括号、引号、分号、冒号等符号，"
        "否则 mermaid 会解析失败。\n"
        "严格只输出 JSON，不要 Markdown 围栏："
        '{"summary": "...", "risks": ["..."], "flow": "flowchart TD\\n  A[入口] --> B[处理]"}'
    )
    user = "范围：%s\n\n%s" % (str(label or "改动"), diff_text)
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def review(label, diff_text):
    """请求一次审阅，返回 {ok, summary, risks, flow, error}。"""
    text = str(diff_text or "").strip()
    if not text:
        return {"ok": False, "summary": "", "risks": [], "flow": "",
                "error": "没有可审查的改动"}
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return {"ok": False, "summary": "", "risks": [], "flow": "",
                "error": "未配置接口密钥"}
    msgs = _review_prompt(label, text[:MAX_DIFF_CHARS])
    try:
        raw = _quick_chat(api_key, api_url, api_model, msgs,
                          max_tokens=REVIEW_MAX_TOKENS, source="ide-review",
                          timeout=(10, 120), tries=1, use_cache=False,
                          temperature=0.2, use_light=False)
    except Exception as e:
        return {"ok": False, "summary": "", "risks": [], "flow": "",
                "error": str(e)[:200]}
    out = parse_review(raw)
    out["ok"] = True
    out["error"] = ""
    return out


def commit_message(diff_text, history=""):
    """根据 diff 生成一条 Git 提交信息 (message, error)。"""
    text = str(diff_text or "").strip()
    if not text:
        return "", "没有可提交的改动"
    api_key, api_url, api_model = get_api_conf("chat")
    if not api_key:
        return "", "未配置接口密钥"
    sys = (
        "你是 Git 提交信息生成器。根据下面的改动写一条提交信息：第一行不超过 50 个字符，"
        "用祈使句概括做了什么；需要时空一行再写 1-3 条要点。"
        "不要 Markdown 代码块、不要引号、不要解释、不要重复 diff 内容。"
    )
    user = "最近提交（参考风格）：\n%s\n\n本次改动：\n%s" % (
        str(history or "（无）"), text[:MAX_DIFF_CHARS])
    try:
        raw = _quick_chat(api_key, api_url, api_model,
                          [{"role": "system", "content": sys},
                           {"role": "user", "content": user}],
                          max_tokens=MESSAGE_MAX_TOKENS, source="git-message",
                          timeout=(8, 60), tries=1, use_cache=False,
                          temperature=0.2, use_light=False)
    except Exception as e:
        return "", str(e)[:200]
    msg = _strip_fence(raw).strip().strip('"').strip("'").strip()
    if not msg:
        return "", "模型没有返回提交信息"
    return msg[:4000], ""
