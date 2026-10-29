# -*- coding: utf-8 -*-
"""Skill：用户可增删的能力包，两级加载。

L1：name + description，常驻系统提示（每个约一行）
L2：正文，模型判断任务对口时通过 skill.read 展开
存储：data/skills/<id>/SKILL.md（frontmatter + 正文）

为什么用文件而不是建表：
- 用户可以直接用编辑器改，也可以整个目录拷给别人；
- 不碰数据库结构，零迁移；
- frontmatter 与 Claude Skill 保持同一套写法，方便互相移植。

注意：正文只当文本读，不执行其中任何脚本。
"""
import os
import shutil
import threading

from app.config import load_config, save_config
from app.store import DATA_DIR, RESOURCES_DIR

SKILLS_DIR = os.path.join(DATA_DIR, "skills")
PRESETS_DIR = os.path.join(RESOURCES_DIR, "presets", "skills")   # 随项目发布的预设
FILE_NAME = "SKILL.md"
BODY_LIMIT = 6000               # 单次展开的正文上限，超出截断
# 旧版本一次性播种过的六个预设。老配置只有 skills_seeded=True，
# 迁移时把它们视为已处理，避免复活用户删过的旧 Skill。
_LEGACY_PRESETS = ("code-review", "debug-helper", "decision-compare",
                   "doc-digest", "study-coach", "writing-polish")
_BAD_CHARS = set('\\/:*?"<>|')

_lock = threading.Lock()
_cache = {"sig": None, "items": []}


def skills_dir():
    return SKILLS_DIR


def seed_defaults():
    """把 presets 里「还没播种过、也没被用户删除」的 Skill 复制到 data/skills。

    - `skills_seeded_ids`：已处理过的 preset id（含用户自己已有的目录）
    - `skills_deleted`：用户删掉的 preset id，永不复活
    - 老配置只有 `skills_seeded=True`：把六个旧预设视为已处理，
      这样后续新增的预设仍能进入已有安装，又不会复活用户删过的旧预设。
    返回复制的个数。
    """
    if not os.path.isdir(PRESETS_DIR):
        return 0
    cfg = load_config()
    done = set(cfg.get("skills_seeded_ids") or [])
    removed = set(cfg.get("skills_deleted") or [])
    if not done and cfg.get("skills_seeded"):
        done = set(_LEGACY_PRESETS)
    copied = 0
    for sid in sorted(os.listdir(PRESETS_DIR)):
        if not safe_id(sid) or sid in done or sid in removed:
            continue
        src = os.path.join(PRESETS_DIR, sid, FILE_NAME)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(SKILLS_DIR, sid, FILE_NAME)
        if os.path.exists(dst):
            done.add(sid)            # 用户已有同名目录：尊重现有内容
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            done.add(sid)
            copied += 1
        except OSError as e:
            print("[Skill] 预设复制失败 %s：%s" % (sid, e))
    cfg["skills_seeded"] = True
    cfg["skills_seeded_ids"] = sorted(done)
    save_config(cfg)
    return copied


def _parse(text):
    """拆 frontmatter，返回 (meta, body)。

    只认 `key: value` 这一层，不引入 yaml 依赖；冒号后的内容原样保留。
    """
    raw = (text or "").lstrip("\ufeff")
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw.strip()
    meta, end = {}, 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
        if ":" in lines[i]:
            k, v = lines[i].split(":", 1)
            meta[k.strip().lower()] = v.strip()
    if not end:
        return {}, raw.strip()
    return meta, "\n".join(lines[end + 1:]).strip()


def _as_list(value):
    return [x.strip() for x in str(value or "").replace("，", ",").split(",") if x.strip()]


def _read(path, sid):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        print("[Skill] 读取失败 %s：%s" % (path, e))
        return None
    meta, body = _parse(raw)
    return {
        "id": sid,
        "name": str(meta.get("name") or sid).strip(),
        "description": str(meta.get("description") or "").strip(),
        "keywords": _as_list(meta.get("keywords")),
        "enabled": str(meta.get("enabled") or "true").strip().lower() not in ("false", "0", "no"),
        "body": body,
        "text": raw,                 # 原文（含 frontmatter），供设置面板编辑
        "path": path,
    }


def _signature():
    """目录签名：任一 SKILL.md 被增删改都会变，用来判断缓存是否失效。"""
    if not os.path.isdir(SKILLS_DIR):
        return ()
    sig = []
    for entry in sorted(os.scandir(SKILLS_DIR), key=lambda x: x.name):
        if not entry.is_dir():
            continue
        path = os.path.join(entry.path, FILE_NAME)
        if os.path.isfile(path):
            st = os.stat(path)
            sig.append((entry.name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def all_skills(include_disabled=False):
    """返回 Skill 元数据（含正文，但调用方只该用 name / description / id）。"""
    sig = _signature()
    with _lock:
        if _cache["sig"] != sig:
            items = []
            for sid, _mtime, _size in sig:
                it = _read(os.path.join(SKILLS_DIR, sid, FILE_NAME), sid)
                if it:
                    items.append(it)
            _cache["sig"], _cache["items"] = sig, items
        items = list(_cache["items"])
    if include_disabled:
        return items
    off = set(load_config().get("skills_disabled") or [])
    return [x for x in items if x["enabled"] and x["id"] not in off]


def get(sid):
    """按 id（或 name）查找，包含已停用的；是否可用由 enabled() 判断。"""
    key = str(sid or "").strip()
    if not key:
        return None
    for it in all_skills(include_disabled=True):
        if it["id"] == key or it["name"] == key:
            return it
    return None


def enabled(sid):
    it = get(sid)
    if not it or not it["enabled"]:
        return False
    return it["id"] not in set(load_config().get("skills_disabled") or [])


def index_block():
    """L1：只列名称与描述。没有可用 Skill 时返回空串（默认提示逐字节不变）。

    工具名写 `skill_read`（原生 function calling 的真实名字）。文本模式下
    normalize_tool_name 会把它映射回能力 id `skill.read`，两种模式都能用。
    """
    items = all_skills()
    if not items:
        return ""
    lines = [
        "## 可用 Skill（专业流程包，按需展开）",
        "下面的条目只是索引。用户的要求与某个 Skill 对口时，调用 skill_read 读取完整步骤，"
        "再按步骤回答；不要凭描述猜内容。每轮最多展开 1 个，两个都对口就选更具体的那个；"
        "无关话题不要调用。",
    ]
    for it in items:
        lines.append("- %s（id: %s）：%s" % (it["name"], it["id"], it["description"] or "无描述"))
    return "\n".join(lines)


def body(sid, limit=BODY_LIMIT):
    """L2：取正文。超长时截断并注明，避免一次吃满上下文。"""
    it = get(sid)
    if not it:
        return ""
    text = it["body"]
    if limit and len(text) > limit:
        text = text[:limit] + "\n\n（正文超过 %d 字，已截断）" % limit
    return text


def safe_id(sid):
    """校验目录名：挡住路径穿越与 Windows 非法字符。"""
    s = str(sid or "").strip()
    if not s or len(s) > 40 or s in (".", "..") or s[-1] in ". ":
        return ""
    if any(ch in _BAD_CHARS for ch in s):
        return ""
    return s


def save(sid, text):
    """写入（覆盖）一个 Skill，返回空串表示成功。"""
    s = safe_id(sid)
    if not s:
        return "❌ Skill id 不合法（不能为空，不能含 \\ / : * ? \" < > |）"
    path = os.path.join(SKILLS_DIR, s, FILE_NAME)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(text or "").rstrip() + "\n")
        os.replace(tmp, path)        # 原子替换，写到一半崩溃也不会毁掉原文件
    except OSError as e:
        return "❌ 保存失败：%s" % e
    return ""


def delete(sid):
    """删除整个 Skill 目录，返回空串表示成功。"""
    s = safe_id(sid)
    it = get(s) if s else None
    if not it:
        return "❌ 没有这个 Skill"
    folder = os.path.dirname(it["path"])
    if os.path.normcase(os.path.dirname(folder)) != os.path.normcase(os.path.normpath(SKILLS_DIR)):
        return "❌ 拒绝删除：路径不在 skills 目录下"
    try:
        shutil.rmtree(folder)
    except OSError as e:
        return "❌ 删除失败：%s" % e
    # 记墓碑：以后 seed_defaults 不再把这个 id 复制回来
    cfg = load_config()
    removed = [x for x in (cfg.get("skills_deleted") or []) if x != s]
    removed.append(s)
    cfg["skills_deleted"] = removed
    save_config(cfg)
    return ""


def set_enabled(sid, on):
    """启停只改 config.skills_disabled，不改用户写的 SKILL.md。"""
    s = safe_id(sid)
    if not s:
        return "❌ Skill id 不合法"
    cfg = load_config()
    off = [x for x in (cfg.get("skills_disabled") or []) if x != s]
    if not on:
        off.append(s)
    cfg["skills_disabled"] = off
    save_config(cfg)
    return ""


def template(name="新 Skill"):
    """给设置面板「新建」用的空白骨架。"""
    return (
        "---\n"
        "name: %s\n"
        "description: 一句话说明它解决什么问题、什么时候该用\n"
        "keywords: 关键词1, 关键词2\n"
        "---\n\n"
        "## 目标\n\n"
        "## 每轮必做\n\n"
        "1. \n\n"
        "## 禁止\n\n"
        "- \n" % name
    )
