# -*- coding: utf-8 -*-
"""表达复盘：把「用户在纠正上一轮」的现场记下来，供输入预检复用。

为什么只记不总结：总结要多调一次模型，而且容易把用户原话改味。
这里只存「上一轮原话 + 这次的纠正语」，预检时作为 few-shot 上下文喂给轻量模型，
让它知道这位用户容易在哪类说法上产生歧义。
"""
import json
import os
import re
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(_ROOT, "data", "expression_tips.json")
MAX_TIPS = 20
_lock = threading.Lock()

# 用户表达「你理解错了 / 我指的不是这个」的典型说法
_CORRECT_RE = re.compile(
    r"不是(?:这个|这|我(?:说|要|指)的)|我没说|理解错|误解|搞错|改错|弄错|"
    r"我是说|我的意思|不是让你|不要(?:动|改|碰)|别(?:动|改|碰)|重(?:来|新做)")
# 太短的纠正语多半是语气词，不记
_MIN_FIX = 4


def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def _prev_user_msg(s):
    for m in reversed((s or {}).get("messages") or []):
        if m.get("role") == "user" and str(m.get("content") or "").strip():
            return str(m.get("content") or "").strip()
    return ""


def note_correction(text, s):
    """这次的消息像在纠正上一轮 → 记一条 (上次原话, 这次的纠正)。

    只在「上一轮也是用户指令」时记；闲聊不记。返回是否写入。
    """
    fix = str(text or "").strip()
    if len(fix) < _MIN_FIX or not _CORRECT_RE.search(fix):
        return False
    prev = _prev_user_msg(s)
    if len(prev) < 6:
        return False
    with _lock:
        d = _load()
        tips = d.get("tips") if isinstance(d.get("tips"), list) else []
        # 同样的「上次 + 这次」只留一条，重复出现时累加次数
        for t in tips:
            if t.get("prev") == prev and t.get("fix") == fix:
                t["count"] = int(t.get("count") or 1) + 1
                t["at"] = time.strftime("%Y-%m-%d %H:%M")
                break
        else:
            tips.append({"at": time.strftime("%Y-%m-%d %H:%M"),
                         "prev": prev[:800], "fix": fix[:400], "count": 1})
        d["tips"] = tips[-MAX_TIPS:]
        try:
            _save(d)
        except Exception:
            return False
    return True


def recent(n=3):
    """最近 n 条复盘（旧的在前），给预检当上下文。"""
    tips = _load().get("tips") or []
    return [t for t in tips[-max(1, int(n)):] if t.get("prev") and t.get("fix")]
