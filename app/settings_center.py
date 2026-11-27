# -*- coding: utf-8 -*-
"""设置中心：校验、持久化、历史快照与回滚。

设计目标（对应设置模块重构）：
- 所有写入都走同一入口，先校验再落盘，避免错误值直接写进 config.json；
- 每次真正产生变更的保存都把「保存前」的完整配置快照到 data/settings_history/，
  保留最近 HISTORY_LIMIT 个版本，支持查看差异与回滚；
- 历史快照里的密钥脱敏（api_key / sessdata），回滚时沿用当前密钥，不把旧密钥写回；
- 旧配置在启动时迁移一次：先备份 config.json，再补 config_version，未知键原样保留。

`/api/config` 仍然可用（内部走本模块的 save_patch），新接口 `/api/settings` 提供
history / diff / rollback / validate。
"""
import copy
import os
import re
import shutil
import time
import uuid

from app.config import DEFAULT_CONFIG, load_config, save_config
from app.store import CONFIG_FILE, DATA_DIR, read_json, write_json

SETTINGS_VERSION = 2
HISTORY_DIR = os.path.join(DATA_DIR, "settings_history")
HISTORY_LIMIT = 50
LEVELS = ("basic", "advanced", "expert")
# 与前端 core.js 的 SECRET_SETTING_KEY 保持一致
SECRET_KEY_RE = re.compile(r"(^|_)(api_key|sessdata)$", re.I)

# 显式范围 / 枚举规则：只收「错了会明显影响运行」的项，其余按默认值类型校验，
# 避免把用户自定义的值（如 -1 = 无上限）误判成错误。
_RULES = {
    "settings_level": {"type": "choice", "choices": LEVELS},
    "usage_daily_tokens": {"type": "int", "min": 0},
    "chat_context_limit": {"type": "int", "min": 0},
    "chat_max_tokens": {"type": "int", "min": 0},
    "chat_round_retries": {"type": "int", "min": 0, "max": 20},
    "chat_upload_retries": {"type": "int", "min": 0, "max": 20},
    "chat_retry_max_minutes": {"type": "num", "min": 0},
    "agent_auto_continue": {"type": "int", "min": -1, "max": 10},
    "hybrid_keep_ratio": {"type": "num", "min": 0, "max": 1},
    "hybrid_quality_gate": {"type": "num", "min": 0, "max": 1},
    "local_keep_ratio": {"type": "num", "min": 0, "max": 1},
}


def is_secret(key):
    return bool(SECRET_KEY_RE.search(str(key or "")))


def _redact_value(key, value):
    if is_secret(key):
        return "***"
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value("", v) for v in value]
    return copy.deepcopy(value)


def redact(cfg):
    """历史 / 差异展示用的脱敏副本（递归处理 presets 等嵌套结构）。"""
    return {key: _redact_value(key, value) for key, value in (cfg or {}).items()}


def _merge_secret_tree(target, current):
    """回滚时递归恢复密钥：历史里是 ***，必须用当前值覆盖（没有就清空）。

    针对 chat_presets / image_presets 这类嵌套结构，不能只处理顶层键。
    """
    if isinstance(target, dict) and isinstance(current, dict):
        for key in list(target):
            if is_secret(key):
                target[key] = current.get(key, "")
            elif isinstance(target[key], (dict, list)):
                _merge_secret_tree(target[key], current.get(key))
        for key, value in current.items():
            if is_secret(key):
                target.setdefault(key, value)
    elif isinstance(target, list) and isinstance(current, list):
        for i, item in enumerate(target):
            if i < len(current):
                _merge_secret_tree(item, current[i])
    return target


def _merge_secrets(target, current):
    """兼容旧调用：顶层 + 嵌套密钥都按当前值恢复。"""
    return _merge_secret_tree(target, current)


# ------------------------------------------------------------
# 校验
# ------------------------------------------------------------
def _as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _check_rule(key, value, rule):
    kind = rule.get("type")
    if kind == "choice":
        if str(value) not in rule.get("choices", ()):
            return "只能是：%s" % " / ".join(rule.get("choices", ()))
        return ""
    if kind in ("int", "num"):
        num = _as_number(value)
        if num is None:
            return "必须是数字"
        if kind == "int" and abs(num - round(num)) > 1e-9:
            return "必须是整数"
        if "min" in rule and num < rule["min"]:
            return "不能小于 %s" % rule["min"]
        if "max" in rule and num > rule["max"]:
            return "不能大于 %s" % rule["max"]
    return ""


def _check_like_default(key, value, default):
    """按默认值类型做宽松校验：兼容旧客户端传来的字符串 / 0/1。"""
    if default is None:
        return ""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return ""
        if value in (0, 1, "0", "1"):
            return ""
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no", "on", "off"):
            return ""
        return "必须是布尔值"
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        num = _as_number(value)
        if num is None:
            return "必须是数字"
        if isinstance(default, int) and abs(num - round(num)) > 1e-9:
            return "必须是整数"
        return ""
    if isinstance(default, dict):
        return "" if isinstance(value, dict) else "必须是对象"
    if isinstance(default, list):
        return "" if isinstance(value, (list, tuple, str)) else "必须是数组"
    if isinstance(default, str):
        return "" if isinstance(value, (str, int, float)) and not isinstance(value, bool) else "必须是字符串"
    return ""


def validate_patch(patch):
    """校验一份增量配置，返回 [{key, message}]；空列表 = 通过。

    未知键不报错：设置项会随版本新增，旧客户端写回整份配置时不能因此失败。
    """
    if not isinstance(patch, dict):
        return [{"key": "", "message": "配置必须是一个对象"}]
    errors = []
    for key, value in patch.items():
        rule = _RULES.get(key)
        msg = _check_rule(key, value, rule) if rule else (
            _check_like_default(key, value, DEFAULT_CONFIG[key]) if key in DEFAULT_CONFIG else "")
        if msg:
            errors.append({"key": key, "message": msg})
    return errors


# ------------------------------------------------------------
# 历史快照
# ------------------------------------------------------------
def _history_files():
    try:
        names = sorted(os.listdir(HISTORY_DIR))
    except OSError:
        return []
    return [os.path.join(HISTORY_DIR, n) for n in names if n.endswith(".json")]


def _write_history(cfg, changed):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    hid = "%d-%s" % (int(time.time() * 1000), uuid.uuid4().hex[:6])
    payload = {
        "id": hid,
        "ts": time.time(),
        "changed": sorted(str(k) for k in (changed or [])),
        "config": redact(cfg),
    }
    try:
        write_json(os.path.join(HISTORY_DIR, hid + ".json"), payload,
                   indent=None, remember=False)
    except OSError as e:
        print("[设置] 历史快照写入失败：%s" % e)
        return ""
    files = _history_files()
    for path in files[:-HISTORY_LIMIT]:
        try:
            os.remove(path)
        except OSError:
            pass
    return hid


def load_version(version_id):
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(version_id or ""))
    if not safe:
        return None
    data = read_json(os.path.join(HISTORY_DIR, safe + ".json"))
    return data if isinstance(data, dict) else None


def history(limit=HISTORY_LIMIT):
    """最近的保存前快照（新 → 旧），只含元信息，不含配置正文。"""
    out = []
    for path in reversed(_history_files()):
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        out.append({
            "id": data.get("id") or os.path.splitext(os.path.basename(path))[0],
            "ts": data.get("ts") or 0,
            "changed": data.get("changed") or [],
        })
        if len(out) >= max(1, int(limit or HISTORY_LIMIT)):
            break
    return out


def diff(version_id):
    """某个历史快照与当前配置的差异（值已脱敏）。"""
    ver = load_version(version_id)
    if not ver:
        return None
    snap = ver.get("config") or {}
    cur = redact(load_config())
    rows = []
    for key in ver.get("changed") or []:
        rows.append({"key": key, "before": snap.get(key), "after": cur.get(key)})
    return {"id": ver.get("id"), "ts": ver.get("ts"), "changed": rows}


# ------------------------------------------------------------
# 保存 / 回滚
# ------------------------------------------------------------
def save_patch(patch, source="api"):
    """校验并保存一份增量配置；返回 {ok, errors, history_id, changed}。

    只校验**本次真正改了值**的键：旧配置里已经存在的越界值不应阻塞无关修改，
    否则用户会因为一条历史脏数据而完全无法保存设置。
    """
    patch = patch if isinstance(patch, dict) else {}
    current = load_config()
    before = copy.deepcopy(current)
    merged = dict(current)
    merged.update(patch)
    changed = [k for k in patch if before.get(k) != merged.get(k)]
    effective = {k: merged[k] for k in changed}
    errors = validate_patch(effective)
    if errors:
        return {"ok": False, "errors": errors, "history_id": "", "changed": []}
    hid = _write_history(before, changed) if changed else ""
    try:
        save_config(merged)
    except OSError as e:
        return {"ok": False, "errors": [{"key": "", "message": "写入失败：%s" % e}],
                "history_id": "", "changed": []}
    return {"ok": True, "errors": [], "history_id": hid,
            "changed": changed, "source": source}


def rollback(version_id):
    """恢复到一个历史快照；密钥沿用当前值。返回 {ok, config?, errors?}。"""
    ver = load_version(version_id)
    if not ver:
        return {"ok": False, "errors": [{"key": "", "message": "找不到这个历史版本"}]}
    current = load_config()
    target = copy.deepcopy(ver.get("config") or {})
    _merge_secrets(target, current)
    changed = [k for k in set(current) | set(target)
               if current.get(k) != target.get(k)]
    errors = validate_patch({k: target.get(k) for k in changed if k in target})
    if errors:
        return {"ok": False, "errors": errors}
    _write_history(current, changed)        # 回滚本身也可撤销
    try:
        save_config(target)
    except OSError as e:
        return {"ok": False, "errors": [{"key": "", "message": "写入失败：%s" % e}]}
    return {"ok": True, "config": load_config(), "changed": changed}


# ------------------------------------------------------------
# 旧配置迁移
# ------------------------------------------------------------
def ensure_migrated():
    """启动时迁移一次：备份 config.json → 补默认 / 旧键改名 → 写 config_version。

    未知键原样保留；已经是当前版本时不做任何写入（幂等）。
    """
    raw = read_json(CONFIG_FILE)
    if not isinstance(raw, dict):
        data = load_config()
        data["config_version"] = SETTINGS_VERSION
        save_config(data)
        return {"migrated": False, "created": True, "backup": ""}
    try:
        ver = int(raw.get("config_version") or 1)
    except (TypeError, ValueError):
        ver = 1
    if ver >= SETTINGS_VERSION:
        return {"migrated": False, "created": False, "backup": ""}
    backup = CONFIG_FILE + ".backup-" + time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copy2(CONFIG_FILE, backup)
    except OSError as e:
        print("[设置] 迁移备份失败（继续写入）：%s" % e)
        backup = ""
    data = load_config()                    # 补默认值 + 旧键改名
    data["config_version"] = SETTINGS_VERSION
    save_config(data)
    print("[设置] 配置已迁移到 v%d（备份：%s）" % (SETTINGS_VERSION, backup or "无"))
    return {"migrated": True, "from": ver, "to": SETTINGS_VERSION, "backup": backup}
