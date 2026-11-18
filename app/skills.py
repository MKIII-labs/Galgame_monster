# -*- coding: utf-8 -*-
"""Skill：用户可增删的能力包，三层按需加载。

L1：name + description + 参考文件清单，常驻系统提示（每个约一行）
L2：正文，模型判断任务对口时通过 skill.read 展开（≤ BODY_LIMIT 字）
L3：references/*.md，正文里指到哪份就读哪份（skill.read 的 file 参数）

存储：data/skills/<id>/SKILL.md（frontmatter + 正文）+ 可选 references/。
scripts/ 与 assets/ 只允许放在技能目录里供人查看，运行时既不读取也不执行。

为什么用文件而不是建表：
- 用户可以直接用编辑器改，也可以整个目录打包给别人；
- 不碰数据库结构，零迁移；
- frontmatter 与 Claude Skill 保持同一套写法，方便互相移植。

注意：正文与参考文件都只当文本读，不执行其中任何脚本。
"""
import io
import json
import os
import re
import shutil
import threading
import time
import zipfile

from app.config import load_config, save_config
from app.store import DATA_DIR, RESOURCES_DIR
from app.textmatch import tokens as textmatch_tokens

SKILLS_DIR = os.path.join(DATA_DIR, "skills")
PRESETS_DIR = os.path.join(RESOURCES_DIR, "presets", "skills")   # 随项目发布的预设
FILE_NAME = "SKILL.md"
REF_DIR = "references"
BODY_LIMIT = 8000               # L2 单次展开的正文上限，超出截断
REF_LIMIT = 12000               # L3 单个参考文件的读取上限
MAX_REF_BYTES = 512 * 1024      # 单个参考文件最大体积
MAX_REF_FILES = 40              # 一个 Skill 最多多少个参考文件
MAX_ZIP_BYTES = 2 * 1024 * 1024 # 导入 zip 的解压总量上限
PRESET_VERSION = 3              # 预设规范版本：升级时按新规范覆盖（先备份）
REF_SUFFIX = (".md", ".txt")
# 旧版本一次性播种过的六个预设。老配置只有 skills_seeded=True，
# 迁移时把它们视为已处理，避免复活用户删过的旧 Skill。
_LEGACY_PRESETS = ("code-review", "debug-helper", "decision-compare",
                   "doc-digest", "study-coach", "writing-polish")
_BAD_CHARS = set('\\/:*?"<>|')

_lock = threading.Lock()
_cache = {"sig": None, "items": []}


def skills_dir():
    return SKILLS_DIR


def _stats_path():
    # 放在 skills 目录里：测试把 SKILLS_DIR 指到 tmp 时统计也一起隔离
    return os.path.join(SKILLS_DIR, ".stats.json")


def _copy_preset(src_dir, dst_dir):
    """复制一个预设：只拷 SKILL.md 与 references/ 下的 .md/.txt。"""
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy2(os.path.join(src_dir, FILE_NAME), os.path.join(dst_dir, FILE_NAME))
    ref_src = os.path.join(src_dir, REF_DIR)
    if not os.path.isdir(ref_src):
        return
    ref_dst = os.path.join(dst_dir, REF_DIR)
    os.makedirs(ref_dst, exist_ok=True)
    for name in sorted(os.listdir(ref_src)):
        if safe_ref_name(name) and os.path.isfile(os.path.join(ref_src, name)):
            shutil.copy2(os.path.join(ref_src, name), os.path.join(ref_dst, name))


def _upgrade_presets(cfg):
    """预设规范升级：把本地已有的预设按新规范覆盖（覆盖前先备份）。

    只处理「预设 id」——用户自己新建的 Skill 不受影响；用户删过的预设不复活。
    覆盖会先写 `<SKILL.md>.bak-<时间戳>`，误覆盖时还能找回原文。
    返回覆盖的个数。
    """
    try:
        done = int(cfg.get("skills_preset_version") or 1)
    except (TypeError, ValueError):
        done = 1
    if done >= PRESET_VERSION:
        return 0
    copied = 0
    removed = set(cfg.get("skills_deleted") or [])
    if os.path.isdir(PRESETS_DIR):
        for sid in sorted(os.listdir(PRESETS_DIR)):
            src_dir = os.path.join(PRESETS_DIR, sid)
            src = os.path.join(src_dir, FILE_NAME)
            dst_dir = os.path.join(SKILLS_DIR, sid)
            dst = os.path.join(dst_dir, FILE_NAME)
            if not safe_id(sid) or sid in removed:
                continue
            if not (os.path.isfile(src) and os.path.isfile(dst)):
                continue
            try:
                shutil.copy2(dst, dst + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
                _copy_preset(src_dir, dst_dir)
                copied += 1
            except OSError as e:
                print("[Skill] 预设升级失败 %s：%s" % (sid, e))
    cfg["skills_preset_version"] = PRESET_VERSION
    save_config(cfg)
    return copied


def seed_defaults():
    """把 presets 里「还没播种过、也没被用户删除」的 Skill 复制到 data/skills。

    - `skills_seeded_ids`：已处理过的 preset id（含用户自己已有的目录）
    - `skills_deleted`：用户删掉的 preset id，永不复活
    - 老配置只有 `skills_seeded=True`：把六个旧预设视为已处理，
      这样后续新增的预设仍能进入已有安装，又不会复活用户删过的旧预设。
    - `skills_preset_version`：预设规范版本，低于当前版本时按新规范覆盖已有预设。
    返回复制的个数。
    """
    cfg = load_config()
    _upgrade_presets(cfg)
    if not os.path.isdir(PRESETS_DIR):
        return 0
    done = set(cfg.get("skills_seeded_ids") or [])
    removed = set(cfg.get("skills_deleted") or [])
    if not done and cfg.get("skills_seeded"):
        done = set(_LEGACY_PRESETS)
    copied = 0
    for sid in sorted(os.listdir(PRESETS_DIR)):
        if not safe_id(sid) or sid in done or sid in removed:
            continue
        src_dir = os.path.join(PRESETS_DIR, sid)
        if not os.path.isfile(os.path.join(src_dir, FILE_NAME)):
            continue
        dst_dir = os.path.join(SKILLS_DIR, sid)
        if os.path.exists(os.path.join(dst_dir, FILE_NAME)):
            done.add(sid)            # 用户已有同名目录：尊重现有内容
            continue
        try:
            _copy_preset(src_dir, dst_dir)
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


def _reference_files(folder):
    """列出 references/ 下可读的参考文件：[{name, size}]。"""
    ref_dir = os.path.join(folder, REF_DIR)
    try:
        names = sorted(os.listdir(ref_dir))
    except OSError:
        return []
    out = []
    for name in names:
        full = os.path.join(ref_dir, name)
        if os.path.isfile(full) and name.lower().endswith(REF_SUFFIX):
            out.append({"name": name, "size": os.path.getsize(full)})
    return out[:MAX_REF_FILES]


def _read(path, sid):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        print("[Skill] 读取失败 %s：%s" % (path, e))
        return None
    meta, body = _parse(raw)
    folder = os.path.dirname(path)
    return {
        "id": sid,
        "name": str(meta.get("name") or sid).strip(),
        "description": str(meta.get("description") or "").strip(),
        "keywords": _as_list(meta.get("keywords")),
        "when_not": _as_list(meta.get("when_not")),
        "enabled": str(meta.get("enabled") or "true").strip().lower() not in ("false", "0", "no"),
        "body": body,
        "text": raw,                 # 原文（含 frontmatter），供设置面板编辑
        "path": path,
        "folder": folder,
        "files": _reference_files(folder),
    }


def _dir_signature(folder):
    """单个技能目录的签名：SKILL.md 与各参考文件任一改动都会变。"""
    parts = []
    for rel in [FILE_NAME] + [os.path.join(REF_DIR, f["name"])
                              for f in _reference_files(folder)]:
        try:
            st = os.stat(os.path.join(folder, rel))
        except OSError:
            continue
        parts.append((rel, st.st_mtime_ns, st.st_size))
    return tuple(parts)


def _signature():
    """目录签名：任一 SKILL.md / 参考文件被增删改都会变，用来判断缓存是否失效。"""
    if not os.path.isdir(SKILLS_DIR):
        return ()
    sig = []
    for entry in sorted(os.scandir(SKILLS_DIR), key=lambda x: x.name):
        if not entry.is_dir():
            continue
        if not os.path.isfile(os.path.join(entry.path, FILE_NAME)):
            continue
        sig.append((entry.name, _dir_signature(entry.path)))
    return tuple(sig)


def all_skills(include_disabled=False):
    """返回 Skill 元数据（含正文与参考文件清单，调用方通常只该用 name / description / files）。"""
    sig = _signature()
    with _lock:
        if _cache["sig"] != sig:
            items = []
            for sid, _sig in sig:
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
    """L1：名称 + 描述 + 参考文件清单。没有可用 Skill 时返回空串。

    工具名写 `skill_read`（原生 function calling 的真实名字）。文本模式下
    normalize_tool_name 会把它映射回能力 id `skill.read`，两种模式都能用。
    """
    items = all_skills()
    if not items:
        return ""
    lines = [
        "## 可用 Skill（专业流程包，按需展开）",
        "任务开始前先扫一遍这里的条目：用户的要求与某个 Skill 的「何时用」对得上时，"
        "**先**调用 skill_read 读取完整步骤，再按步骤回答；不要凭描述猜内容。",
        "- 多个命中时，流程 / 方法论类优先，具体实现类其次；开始前说一句「使用 <名称> 处理」。",
        "- 涉及改代码 / 改文件时，优先看「先读后改」「验证先行」「安全重构」这几个 Skill。",
        "- 每轮最多展开 1 个；无关话题不要调用。",
        "- 正文里指到 references/ 下的文件时，用 skill_read(file=\"references/文件名\") 单独读取。",
        "- 正文建议包含：何时用 / 不适用 / 核心步骤 / 输出格式 / 失败处理 / 常见错误。",
        "- 条目格式：名称（id）：何时用；有参考文件时附「参考：…」；有 when_not 时附「不适用：…」。",
    ]
    for it in items:
        tail = ""
        if it["files"]:
            tail = "；参考：" + "、".join(f["name"] for f in it["files"][:4])
        if it.get("when_not"):
            tail += "；不适用：" + "、".join(it["when_not"][:3])
        lines.append("- %s（id: %s）：%s%s"
                     % (it["name"], it["id"], it["description"] or "无描述", tail))
    return "\n".join(lines)


def body(sid, limit=BODY_LIMIT):
    """L2：取正文。超长时截断并注明，避免一次吃满上下文。"""
    it = get(sid)
    if not it:
        return ""
    text = it["body"]
    if limit and len(text) > limit:
        text = text[:limit] + "\n\n（正文超过 %d 字，已截断；更长的内容应拆到 references/）" % limit
    return text


def safe_ref_name(name):
    """校验参考文件名：只允许 references/ 下的 .md / .txt，挡掉路径穿越。"""
    s = str(name or "").strip().replace("\\", "/")
    if s.lower().startswith(REF_DIR + "/"):
        s = s[len(REF_DIR) + 1:]
    if not s or len(s) > 80 or "/" in s or s in (".", "..") or s.startswith("."):
        return ""
    if any(ch in _BAD_CHARS for ch in s):
        return ""
    if not s.lower().endswith(REF_SUFFIX):
        return ""
    return s


def read_reference(sid, name, limit=REF_LIMIT):
    """L3：读取一个参考文件；不存在 / 不合法返回空串。"""
    it = get(sid)
    fname = safe_ref_name(name)
    if not it or not fname:
        return ""
    hit = next((f for f in it["files"] if f["name"].lower() == fname.lower()), None)
    if not hit:
        return ""
    path = os.path.join(it["folder"], REF_DIR, hit["name"])
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print("[Skill] 参考文件读取失败 %s：%s" % (path, e))
        return ""
    if limit and len(text) > limit:
        text = text[:limit] + "\n\n（参考文件超过 %d 字，已截断）" % limit
    return text


def save_reference(sid, name, text):
    """写入（覆盖）一个参考文件，返回空串表示成功。"""
    it = get(sid)
    if not it:
        return "❌ 没有这个 Skill"
    fname = safe_ref_name(name)
    if not fname:
        return "❌ 参考文件名不合法（只能是 references/ 下的 .md / .txt）"
    content = str(text or "")
    if len(content.encode("utf-8")) > MAX_REF_BYTES:
        return "❌ 参考文件超过 %d KB" % (MAX_REF_BYTES // 1024)
    path = os.path.join(it["folder"], REF_DIR, fname)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content.rstrip() + "\n")
        os.replace(tmp, path)
    except OSError as e:
        return "❌ 保存失败：%s" % e
    return ""


def delete_reference(sid, name):
    """删除一个参考文件，返回空串表示成功。"""
    it = get(sid)
    fname = safe_ref_name(name)
    if not it or not fname:
        return "❌ 参考文件名不合法"
    hit = next((f for f in it["files"] if f["name"].lower() == fname.lower()), None)
    if not hit:
        return "❌ 没有这个参考文件"
    try:
        os.remove(os.path.join(it["folder"], REF_DIR, hit["name"]))
    except OSError as e:
        return "❌ 删除失败：%s" % e
    return ""


# ------------------------------------------------------------
# 触发匹配：给「本轮可能相关的 Skill」提示用
# ------------------------------------------------------------
def trigger_matches(text, limit=3):
    """按名称 / 关键词匹配本轮消息，返回 [(id, name, score)]。

    只做「提示」，不改变工具目录：命中后模型仍需自己判断是否调用 skill_read。
    打分刻意保守（关键词或名称命中才过线），避免每轮都塞一堆无关提示。
    """
    low = str(text or "").lower()
    if not low.strip():
        return []
    q_tokens = set(textmatch_tokens(low, 64))
    out = []
    for it in all_skills():
        score = 0
        name = it["name"].lower()
        if name and name in low:
            score += 6
        for kw in it["keywords"]:
            k = kw.lower()
            if len(k) >= 2 and k in low:
                score += 3
        name_hits = set(textmatch_tokens(it["name"], 16)) & q_tokens
        score += min(6, len(name_hits) * 2)
        if score >= 3:
            out.append((score, it["id"], it["name"]))
    out.sort(reverse=True)
    return [(sid, name, score) for score, sid, name in out[:limit]]


def trigger_hint(text, limit=2):
    """本轮可能相关的 Skill：一行提示，贴到请求末尾（不进 system，免得破坏前缀缓存）。"""
    hits = trigger_matches(text, limit=limit)
    if not hits:
        return ""
    names = "、".join("%s（id: %s）" % (name, sid) for sid, name, _score in hits)
    return ("【本轮可能相关的 Skill】%s。如果确实对口，先调用 skill_read 读取完整步骤再回答；"
            "不相关就忽略这一行。" % names)


# ------------------------------------------------------------
# 作者校验
# ------------------------------------------------------------
_WORKFLOW_WORDS = re.compile(r"步骤|流程|第一步|然后|最后|先.{0,12}再")
_TRIGGER_WORDS = re.compile(r"use when|当|如果|遇到|需要|要求|提到|涉及", re.I)


def lint_text(text, sid=""):
    """检查一份 SKILL.md，返回 [{level, field, message}]；空列表不应出现（至少一条 ok）。"""
    out = []

    def add(level, field, message):
        out.append({"level": level, "field": field, "message": message})

    raw = str(text or "").lstrip("\ufeff")
    meta, body_text = _parse(raw)
    if not meta:
        add("error", "frontmatter", "缺少 frontmatter：文件开头要用 --- 包住 name / description")
    name = str(meta.get("name") or "").strip()
    desc = str(meta.get("description") or "").strip()
    if not name:
        add("error", "name", "缺少 name")
    elif len(name) > 40:
        add("warn", "name", "name 超过 40 字，列表里会被截断")
    if not desc:
        add("error", "description", "缺少 description：模型靠它判断什么时候该展开")
    else:
        if len(desc) > 160:
            add("warn", "description", "description 有 %d 字，建议 ≤160 字（每个 Skill 每轮都占提示词）"
                % len(desc))
        if _WORKFLOW_WORDS.search(desc):
            add("warn", "description",
                "description 只写「何时用」，不要总结流程：模型会照描述走捷径、不读正文")
        if not _TRIGGER_WORDS.search(desc):
            add("info", "description", "建议写成「当…时使用」，把触发条件写清楚")
    if not body_text:
        add("warn", "body", "正文为空")
    else:
        if len(body_text) > BODY_LIMIT:
            add("warn", "body", "正文 %d 字，超过 %d 字；长内容请拆到 references/"
                % (len(body_text), BODY_LIMIT))

        def _has_section(title):
            return bool(re.search(r"^##\s*%s\s*$" % re.escape(title),
                                  body_text, re.M))

        if not _has_section("何时用"):
            add("warn", "body", "建议保留「## 何时用」：写清触发条件与不触发条件")
        if not _has_section("核心步骤"):
            add("warn", "body", "建议保留「## 核心步骤」：写成可执行的分步指令")
        if not (_has_section("输出格式") or _has_section("规则")):
            add("info", "body", "建议补「## 输出格式」或「## 规则」：写清交付形态")
        if not _has_section("不适用"):
            add("info", "body", "建议补「## 不适用」：写清什么时候不要用这个 Skill")
        if not _has_section("失败处理"):
            add("info", "body", "建议补「## 失败处理」：写清失败 / 缺信息时怎么退出")
        if not _has_section("常见错误"):
            add("info", "body", "建议补「## 常见错误」：写清最容易犯的错")
    if not _as_list(meta.get("when_not")):
        add("info", "when_not",
            "建议补 when_not：用逗号列出不该触发这个 Skill 的场景")
    if not _as_list(meta.get("keywords")):
        add("info", "keywords", "建议补 keywords：用于每轮触发提示与搜索")
    if sid and not safe_id(sid):
        add("error", "id", "id 不合法：不能含 \\ / : * ? \" < > |，长度不超过 40")
    if not any(x["level"] in ("error", "warn", "info") for x in out):
        add("ok", "all", "检查通过")
    return out


def lint(sid):
    it = get(sid)
    if not it:
        return [{"level": "error", "field": "id", "message": "没有这个 Skill"}]
    return lint_text(it["text"], it["id"])


# ------------------------------------------------------------
# 使用统计（给设置面板看：哪些 Skill 真被用过）
# ------------------------------------------------------------
_stats_lock = threading.Lock()


def _load_stats():
    try:
        with open(_stats_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_use(sid):
    """记一次 skill_read（按 id）。失败只打印，不阻断工具。"""
    key = safe_id(sid)
    if not key:
        return
    with _stats_lock:
        data = _load_stats()
        row = data.get(key) if isinstance(data.get(key), dict) else {}
        row["count"] = int(row.get("count") or 0) + 1
        row["last"] = time.time()
        data[key] = row
        try:
            os.makedirs(SKILLS_DIR, exist_ok=True)
            tmp = _stats_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=None)
            os.replace(tmp, _stats_path())
        except OSError as e:
            print("[Skill] 统计写入失败：%s" % e)


def stats():
    return _load_stats()


# ------------------------------------------------------------
# 导入 / 导出
# ------------------------------------------------------------
def _slug(name):
    s = re.sub(r"[^a-z0-9_-]+", "-", str(name or "").lower()).strip("-")[:40]
    return s or "skill"


def export_zip(sid):
    """把一个 Skill 打成 zip（SKILL.md + references/），返回 bytes。"""
    it = get(sid)
    if not it:
        raise ValueError("没有这个 Skill")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(it["path"], FILE_NAME)
        for f in it["files"]:
            z.write(os.path.join(it["folder"], REF_DIR, f["name"]),
                    REF_DIR + "/" + f["name"])
    return buf.getvalue()


def _validate_bundle(names, sizes):
    """检查 zip / 目录里的相对路径是否只含 SKILL.md 与 references/*.md|txt。"""
    has_main = False
    total = 0
    for rel, size in zip(names, sizes):
        rel = rel.replace("\\", "/").lstrip("./")
        if rel == FILE_NAME:
            has_main = True
        elif rel.startswith(REF_DIR + "/") and safe_ref_name(rel):
            pass
        else:
            return "❌ 只允许 SKILL.md 与 references/ 下的 .md / .txt：%s" % rel
        if size > MAX_REF_BYTES:
            return "❌ 文件过大：%s" % rel
        total += size
    if not has_main:
        return "❌ 缺少 SKILL.md"
    if total > MAX_ZIP_BYTES:
        return "❌ 内容超过 %d KB" % (MAX_ZIP_BYTES // 1024)
    return ""


def _write_bundle(sid, main_text, refs):
    err = save(sid, main_text)
    if err:
        return err
    for name, text in (refs or {}).items():
        e = save_reference(sid, name, text)
        if e:
            return e
    return ""


def import_zip(data, id_hint=""):
    """导入一个 zip，返回 (新 id, 错误)。id 自动去重，不会覆盖已有 Skill。"""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return "", "❌ 没有收到文件内容"
    if len(data) > MAX_ZIP_BYTES * 2:
        return "", "❌ 压缩包过大"
    try:
        z = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except zipfile.BadZipFile:
        return "", "❌ 不是有效的 zip 文件"
    infos = []
    for info in z.infolist():
        if info.is_dir():
            continue
        rel = info.filename.replace("\\", "/")
        if rel.startswith("/") or ".." in rel.split("/"):
            return "", "❌ 压缩包里有非法路径：%s" % rel
        infos.append((rel, info))
    # 兼容「整个技能目录被打包」的 zip：所有条目共用同一个顶层目录时先剥掉它
    tops = {rel.split("/", 1)[0] for rel, _ in infos if "/" in rel}
    if len(tops) == 1:
        top = next(iter(tops))
        if all(rel.startswith(top + "/") for rel, _ in infos) and \
                any(rel == top + "/" + FILE_NAME for rel, _ in infos):
            infos = [(rel[len(top) + 1:], info) for rel, info in infos]
    names = [rel for rel, _ in infos]
    sizes = [info.file_size for _, info in infos]
    err = _validate_bundle(names, sizes)
    if err:
        return "", err
    main_text, refs = "", {}
    for rel, info in infos:
        try:
            text = z.read(info).decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            return "", "❌ 只能导入 UTF-8 文本：%s" % rel
        if rel == FILE_NAME:
            main_text = text
        else:
            refs[rel.split("/", 1)[1]] = text
    meta, _body = _parse(main_text)
    if not meta.get("name") or not meta.get("description"):
        return "", "❌ SKILL.md 缺少 name / description"
    hint = re.sub(r"\.zip$", "", str(id_hint or ""), flags=re.I)
    sid = unique_id(_slug(hint or meta.get("name") or ""))
    err = _write_bundle(sid, main_text, refs)
    if err:
        return "", err
    return sid, ""


def import_folder(path):
    """从本地文件夹导入一个 Skill，返回 (新 id, 错误)。只读 SKILL.md 与 references/。"""
    folder = os.path.abspath(str(path or "").strip().strip('"'))
    main_path = os.path.join(folder, FILE_NAME)
    if not folder or not os.path.isfile(main_path):
        return "", "❌ 文件夹里没有 SKILL.md：%s" % folder
    try:
        with open(main_path, "r", encoding="utf-8") as f:
            main_text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return "", "❌ 读取失败：%s" % e
    meta, _body = _parse(main_text)
    if not meta.get("name") or not meta.get("description"):
        return "", "❌ SKILL.md 缺少 name / description"
    refs = {}
    ref_dir = os.path.join(folder, REF_DIR)
    if os.path.isdir(ref_dir):
        for name in sorted(os.listdir(ref_dir)):
            full = os.path.join(ref_dir, name)
            if not (safe_ref_name(name) and os.path.isfile(full)):
                continue
            try:
                with open(full, "r", encoding="utf-8") as f:
                    refs[name] = f.read()
            except (OSError, UnicodeDecodeError) as e:
                return "", "❌ 参考文件读取失败 %s：%s" % (name, e)
    sid = unique_id(_slug(meta.get("name") or ""))
    err = _write_bundle(sid, main_text, refs)
    if err:
        return "", err
    return sid, ""


def safe_id(sid):
    """校验目录名：挡住路径穿越与 Windows 非法字符。"""
    s = str(sid or "").strip()
    if not s or len(s) > 40 or s in (".", "..") or s[-1] in ". ":
        return ""
    if any(ch in _BAD_CHARS for ch in s):
        return ""
    return s


def unique_id(base):
    """给「新建」分配一个不撞车的 id：base、base-2、base-3…

    只做 id 计算，不落盘；save() 仍会重新校验。这样连续点「新建」不会互相覆盖
    （旧接口按 name 生成 id，默认名「新 Skill」每次都得到 skill，结果只剩一个 Skill）。
    """
    s = (safe_id(base) or "skill")[:40]
    if not os.path.isdir(os.path.join(SKILLS_DIR, s)):
        return s
    n = 2
    while n <= 999:
        suffix = "-%d" % n
        cand = s[:40 - len(suffix)] + suffix
        if not os.path.isdir(os.path.join(SKILLS_DIR, cand)):
            return cand
        n += 1
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
    """给设置面板「新建」用的骨架：description 只写触发条件，正文按规范分节。"""
    return (
        "---\n"
        "name: %s\n"
        "description: 当用户……时使用（写触发条件，不要写步骤总结）\n"
        "keywords: 关键词1, 关键词2\n"
        "when_not: 不适用场景1, 不适用场景2\n"
        "---\n\n"
        "# %s\n\n"
        "## 何时用\n\n"
        "- \n\n"
        "## 不适用\n\n"
        "- \n\n"
        "## 核心步骤\n\n"
        "1. \n\n"
        "## 输出格式\n\n"
        "- \n\n"
        "## 失败处理\n\n"
        "- 缺少什么信息 / 哪一步失败时，怎么退出或降级\n\n"
        "## 常见错误\n\n"
        "- \n" % (name, name)
    )
