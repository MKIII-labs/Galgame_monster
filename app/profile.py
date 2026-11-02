# -*- coding: utf-8 -*-
"""用户画像读写（user_profile.json）"""
import uuid

from app.store import PROFILE_FILE, read_json, write_json

DEFAULT_PROFILE = {"manual": "", "rules": [], "auto": "", "auto_learn_enabled": True,
                   "learning_prompt_custom": ""}

MAX_RULES = 50            # 规则条数上限
MAX_RULE_CHARS = 4000     # 单条规则正文上限


def load_profile():
    data = read_json(PROFILE_FILE)
    if not isinstance(data, dict):
        return dict(DEFAULT_PROFILE)
    for key, val in DEFAULT_PROFILE.items():
        if key not in data:
            data[key] = val
    return data


def save_profile(profile):
    write_json(PROFILE_FILE, profile)


def clean_rules(raw):
    """把前端 / 旧数据里的规则列表整理成 [{id,text,enabled}]。

    - 兼容纯字符串写法（视为启用）；
    - 空正文直接丢弃；单个规则截断到 MAX_RULE_CHARS；最多 MAX_RULES 条；
    - id 缺失时补一个，方便前端做 key / 排序。
    """
    out = []
    for it in list(raw or [])[:MAX_RULES]:
        if isinstance(it, str):
            text, enabled, rid = it, True, ""
        elif isinstance(it, dict):
            text = it.get("text")
            enabled = it.get("enabled", True)
            rid = str(it.get("id") or "").strip()[:40]
            if str(enabled).strip().lower() in ("false", "0", "no", "off"):
                enabled = False
            enabled = bool(enabled)
        else:
            continue
        text = str(text or "").strip()
        if not text:
            continue
        out.append({"id": rid or ("r_" + uuid.uuid4().hex[:10]),
                    "text": text[:MAX_RULE_CHARS],
                    "enabled": enabled})
    return out


def manual_block(profile):
    """「全局规则」真正注入提示词的文本。

    - 有 rules（哪怕全部停用）→ 只拼启用的规则；全部停用时返回空串，
      不会回退到旧字段把用户刚停用的内容又塞回去；
    - 还没有 rules → 用旧字段 manual，保证老配置行为不变。
    """
    p = profile or {}
    rules = p.get("rules")
    if isinstance(rules, list) and rules:
        parts = [str(r.get("text") or "").strip()
                 for r in rules
                 if isinstance(r, dict) and r.get("enabled", True)]
        return "\n\n".join(x for x in parts if x)
    return str(p.get("manual") or "").strip()

