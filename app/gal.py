# -*- coding: utf-8 -*-
"""GAL 演出模式：角色/立绘资源、输出契约提示词、流式 speech 抽取、回复解析、情绪匹配

设计见 docs/GAL_MODE_DESIGN.md（本地文档）。
本模块只做「数据 + 契约 + 解析」，不涉及 HTTP；路由与聊天流集成在 app/httpd.py。
"""
import base64
import json
import os
import random
import re
import shutil

from app import prompts as P
from app.store import DATA_DIR

GAL_DIR = os.path.join(DATA_DIR, "gal")
CHARS_DIR = os.path.join(GAL_DIR, "characters")

SCHEMA_ID = "gal.dialog.v1"

# 立绘可用图片类型（与 IDE 预览一致）
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
# 音频（立绘绑定台词音频 / BGM）
AUDIO_EXT = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
             ".m4a": "audio/mp4", ".flac": "audio/flac"}
MIME_EXT = dict(IMAGE_EXT)
MIME_EXT.update(AUDIO_EXT)
MAX_ASSET_BYTES = 12 * 1024 * 1024
# GPT-SoVITS 微调权重（.ckpt / .pth）可达数百 MB，单独放宽
MAX_WEIGHT_BYTES = 800 * 1024 * 1024
WEIGHT_EXT = (".ckpt", ".pth")

# 背景 / BGM 资源目录（不属于任何角色）
BACKGROUND_DIR = os.path.join(GAL_DIR, "backgrounds")
BGM_DIR = os.path.join(GAL_DIR, "bgm")
# 背景库索引（图片仍在 backgrounds/ 下，名称与标签记在这里）
BACKGROUND_INDEX = os.path.join(GAL_DIR, "backgrounds.json")
# 背景分类标签的固定维度（顺序即 UI 与提示词里的展示顺序）
BG_TAG_DIMS = ("场景", "时间", "天气", "氛围")
# BGM 库索引与分类标签维度（音频按「情绪 / 场景 / 氛围」归类）
BGM_INDEX = os.path.join(GAL_DIR, "bgm.json")
BGM_TAG_DIMS = ("情绪", "场景", "氛围")


# ============================================================
# 1. 角色资源
# ============================================================
def ensure_dirs():
    os.makedirs(CHARS_DIR, exist_ok=True)


def _valid_cid(cid):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(cid or "")))


def char_dir(cid):
    if not _valid_cid(cid):
        raise ValueError("非法角色 id（只允许字母/数字/下划线/短横线，且不超过 64 字符）")
    ensure_dirs()
    return os.path.join(CHARS_DIR, str(cid))


def _normalize_sprite(it):
    if not isinstance(it, dict):
        return None
    path = str(it.get("path") or "").replace("\\", "/").strip()
    if not path:
        return None
    tags = it.get("emotion_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return {
        "path": path,
        "emotion_tags": [str(t).strip() for t in tags if str(t).strip()],
        "voice_path": str(it.get("voice_path") or "").replace("\\", "/").strip(),
    }


def _to_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _valid_color(v):
    """角色主题色：只接受 #rgb / #rrggbb，其余返回空串（前端回退到主题色）"""
    s = str(v or "").strip()
    return s if re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", s) else ""


def _normalize_tts(data):
    """角色的配音（TTS）预设。

    {active, presets:{名称:{provider,voice,speed,model,url,
                          ref_audio,prompt_text,prompt_lang,
                          gpt_weights,sovits_weights}}}

    - ref_audio：角色目录内相对路径的参考音频（GPT-SoVITS 零样本音色克隆）
    - gpt_weights / sovits_weights：角色目录内相对路径的微调权重（可选，需本地 GPT-SoVITS）
    """
    data = data if isinstance(data, dict) else {}
    presets = {}
    for name, p in (data.get("presets") or {}).items():
        if not isinstance(p, dict):
            continue
        nm = str(name).strip()[:32]
        if not nm:
            continue
        lang = str(p.get("prompt_lang") or "").strip().lower()
        presets[nm] = {
            "provider": str(p.get("provider") or "").strip().lower(),
            "voice": str(p.get("voice") or "").strip(),
            "speed": min(4.0, max(0.25, _to_float(p.get("speed"), 1.0))),
            "model": str(p.get("model") or "").strip(),
            "url": str(p.get("url") or "").strip(),
            "ref_audio": str(p.get("ref_audio") or "").replace("\\", "/").strip(),
            "prompt_text": str(p.get("prompt_text") or "").strip(),
            "prompt_lang": lang if lang in ("zh", "en", "ja") else "",
            "gpt_weights": str(p.get("gpt_weights") or "").replace("\\", "/").strip(),
            "sovits_weights": str(p.get("sovits_weights") or "").replace("\\", "/").strip(),
        }
    active = str(data.get("active") or "").strip()
    if active not in presets:
        active = next(iter(presets), "")
    return {"active": active, "presets": presets}


def normalize_character(data, cid):
    """把外部 JSON 收敛成固定结构（缺字段给默认值）"""
    data = data if isinstance(data, dict) else {}
    sprites = [s for s in (_normalize_sprite(x) for x in (data.get("sprites") or [])) if s]
    scale = min(3.0, max(0.2, _to_float(data.get("sprite_scale"), 1.0)))
    return {
        "id": str(cid),
        "name": str(data.get("name") or cid),
        "color": _valid_color(data.get("color")),
        "sprite_scale": scale,
        "profile": str(data.get("profile") or ""),
        "intro": str(data.get("intro") or "").strip()[:1500],  # 给玩家看的一段介绍（不注入 AI）
        "sprites": sprites,
        "tts": _normalize_tts(data.get("tts")),
    }


def tts_override(char, preset_name=None):
    """取该角色的配音预设（用于覆盖全局 TTS 配置）；没有则返回 None"""
    t = (char or {}).get("tts") or {}
    presets = t.get("presets") or {}
    if not presets:
        return None
    name = str(preset_name or t.get("active") or "").strip() or next(iter(presets), "")
    p = presets.get(name)
    return dict(p, _name=name) if p else None


def load_character(cid):
    if not _valid_cid(cid):
        return None
    p = os.path.join(char_dir(cid), "character.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[GAL] 读取角色失败 {cid}：{e}")
        return None
    return normalize_character(data, cid)


def save_character(cid, data):
    d = char_dir(cid)
    os.makedirs(d, exist_ok=True)
    char = normalize_character(data, cid)
    with open(os.path.join(d, "character.json"), "w", encoding="utf-8") as f:
        json.dump(char, f, ensure_ascii=False, indent=2)
    return char


def delete_character(cid):
    d = char_dir(cid)
    if not os.path.isdir(d):
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def list_characters():
    ensure_dirs()
    out = []
    for name in sorted(os.listdir(CHARS_DIR)):
        p = os.path.join(CHARS_DIR, name)
        if not os.path.isdir(p):
            continue
        c = load_character(name)
        if not c:
            continue
        out.append({
            "id": c["id"], "name": c["name"], "color": c["color"],
            "intro": c.get("intro", ""),
            "sprite_count": len(c["sprites"]),
            "default_sprite": (c["sprites"][0]["path"] if c["sprites"] else ""),
        })
    return out


# ---- 角色包（.char，zip）导入 / 导出 ----
MAX_PACK_BYTES = 1024 * 1024 * 1024        # 社区包常见 300–500MB
PACKS_DIR = os.path.join(GAL_DIR, "packs")  # 待导入的角色包目录
MAX_SPEECH_TOTAL = 80 * 1024 * 1024         # 包内原声台词最多解出这么多
MAX_SPEECH_FILES = 300


def _unique_id(preferred):
    """根据名字生成安全且不冲突的角色 id（目录名）"""
    base = re.sub(r"[^A-Za-z0-9_-]", "", str(preferred or ""))[:48] or "char"
    cid, i = base, 2
    while os.path.isdir(os.path.join(CHARS_DIR, cid)):
        cid = "%s-%d" % (base, i)
        i += 1
    return cid


def _tags_from_filename(fn):
    """从立绘文件名里提取情绪标签（Shinsekai 的立绘名常写成「立绘_01_礼貌拘谨鞠躬.png」）"""
    stem = os.path.splitext(os.path.basename(str(fn)))[0]
    skip = {"立绘", "sprite", "sprites", "表情", "avatar", "头像", "bg", "background"}
    out = []
    for p in re.split(r"[_\-\s()（）\[\]【】,，.]+", stem):
        p = p.strip()
        if not p or p.isdigit() or p.lower() in skip:
            continue
        out.append(p)
    return (out or ([stem] if stem else []))[:6]


def export_character(cid):
    """把角色目录打包成 .char（zip）；返回 (建议文件名, 字节)"""
    import io
    import zipfile
    c = load_character(cid)
    if not c:
        raise ValueError("角色不存在")
    base = char_dir(cid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        have_manifest = False
        for dirpath, _dirs, files in os.walk(base):
            for fn in sorted(files):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base).replace("\\", "/")
                if rel == "manifest.json":
                    have_manifest = True
                z.write(full, rel)
        if not have_manifest:
            z.writestr("manifest.json", json.dumps(
                {"format": "galgame_monster.character", "version": 1}, ensure_ascii=False, indent=2))
    safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", c["name"]).strip("_")[:32] or cid
    return safe + ".char", buf.getvalue()


def _read_pack_meta(zf, names):
    """从 zip 里读取角色元数据；兼容我们的 character.json 与 Shinsekai 的 character.yaml"""
    lower = {n.lower(): n for n in names}
    if "character.json" in lower:
        try:
            return json.loads(zf.read(lower["character.json"]).decode("utf-8", "ignore"))
        except Exception:
            return None
    for cand in ("character.yaml", "character.yml", "config/character.yaml"):
        if cand in lower:
            try:
                import yaml
            except ImportError:
                raise ValueError("导入 Shinsekai 角色包需要 PyYAML（pip install pyyaml）")
            try:
                data = yaml.safe_load(zf.read(lower[cand]).decode("utf-8", "ignore"))
            except Exception:
                return None
            if isinstance(data, list):
                return data[0] if data else None
            return data if isinstance(data, dict) else None
    return None


def import_character_pack(raw=None, filename="", path=None):
    """解析 .char / zip 角色包并落盘为新角色；返回角色 dict。

    支持：本项目的 character.json；Shinsekai 的 character.yaml + sprites/**。
    - 复制 sprites/** 原图（情绪标签从原文件名推断）
    - 复制参考音频 → 角色配音预设（GPT-SoVITS 音色克隆）
    - 复制 speech/** 原声台词 wav（供试听 / 绑定到立绘）
    - 跳过体积巨大的 models/*.ckpt|*.pth 训练权重
    传 path 时直接从磁盘打开 zip（大包不必整体读入内存）。
    """
    import io
    import zipfile
    if path:
        try:
            if os.path.getsize(path) > MAX_PACK_BYTES:
                raise ValueError("角色包过大（>1GB）")
            zf = zipfile.ZipFile(path)
        except ValueError:
            raise
        except Exception:
            raise ValueError("不是有效的 zip 角色包")
    else:
        if not raw:
            raise ValueError("空文件")
        if len(raw) > MAX_PACK_BYTES:
            raise ValueError("角色包过大（>1GB）")
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except Exception:
            raise ValueError("不是有效的 zip 角色包")

    names = [n for n in zf.namelist() if not n.endswith("/")]

    def safe_rel(n):
        p = str(n).replace("\\", "/").lstrip("/")
        if not p or p.startswith("../") or "/../" in p or re.match(r"^[A-Za-z]:", p):
            return ""
        return p

    meta = _read_pack_meta(zf, names)
    if not isinstance(meta, dict):
        meta = {}
    name = str(meta.get("name") or os.path.splitext(os.path.basename(str(filename)))[0] or "导入角色").strip()
    cid = _unique_id(name)
    dest = char_dir(cid)
    os.makedirs(dest, exist_ok=True)

    # 1) 只复制图片（跳过 models/ 与音频，避免几百 MB 的语音模型）
    copied = []
    for n in names:
        rel = safe_rel(n)
        if not rel or os.path.splitext(rel)[1].lower() not in IMAGE_EXT:
            continue
        dst_rel = rel if rel.startswith("sprites/") else "sprites/" + os.path.basename(rel)
        dst = os.path.join(dest, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            with open(dst, "wb") as f:
                f.write(zf.read(n))
        except Exception:
            continue
        copied.append(dst_rel)
    copied.sort()

    # 1b) 参考音频（GPT-SoVITS 音色克隆用）：按 refer_audio_path 的文件名匹配，退化为唯一的 models/*.wav
    ref_rel = ""
    audio_names = [n for n in names if os.path.splitext(n)[1].lower() in AUDIO_EXT]
    want = os.path.basename(str(meta.get("refer_audio_path") or "").replace("\\", "/"))
    cand = ""
    if want:
        for n in audio_names:
            if os.path.basename(n) == want:
                cand = n
                break
    if not cand:
        models_wav = [n for n in audio_names if n.replace("\\", "/").lower().startswith("models/")]
        if len(models_wav) == 1:
            cand = models_wav[0]
    if cand:
        rel = safe_rel(cand)
        if rel and zf.getinfo(cand).file_size <= MAX_ASSET_BYTES:
            dst_rel = "voice/ref" + os.path.splitext(rel)[1].lower()
            dst = os.path.join(dest, dst_rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                with open(dst, "wb") as f:
                    f.write(zf.read(cand))
                ref_rel = dst_rel
            except Exception:
                ref_rel = ""

    # 1c) 包内原声台词（speech/**）：解到 voice/speech/，供试听或绑定到立绘
    speech_files, speech_bytes = 0, 0
    for n in names:
        rel = safe_rel(n)
        if not rel or os.path.splitext(rel)[1].lower() not in AUDIO_EXT:
            continue
        if not rel.lower().startswith("speech/"):
            continue
        info = zf.getinfo(n)
        if (info.file_size > MAX_ASSET_BYTES or speech_files >= MAX_SPEECH_FILES
                or speech_bytes + info.file_size > MAX_SPEECH_TOTAL):
            continue
        dst_rel = "voice/speech/" + os.path.basename(rel)
        dst = os.path.join(dest, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):                     # 同名去重
            stem, ext2 = os.path.splitext(os.path.basename(rel))
            dst_rel = "voice/speech/%s_%d%s" % (stem, speech_files + 1, ext2)
            dst = os.path.join(dest, dst_rel)
        try:
            with open(dst, "wb") as f:
                f.write(zf.read(n))
        except Exception:
            continue
        speech_files += 1
        speech_bytes += info.file_size

    # 1c) 微调权重（.ckpt / .pth）：GPT 与 SoVITS 各取一个，存到 voice/
    gpt_rel, sovits_rel = "", ""
    for n in names:
        rel = safe_rel(n)
        ext = os.path.splitext(rel)[1].lower()
        if not rel or ext not in WEIGHT_EXT:
            continue
        try:
            if zf.getinfo(n).file_size > MAX_WEIGHT_BYTES:
                continue
        except Exception:
            continue
        is_gpt = ext == ".ckpt"
        if is_gpt and gpt_rel:
            continue
        if (not is_gpt) and sovits_rel:
            continue
        dst_rel = "voice/" + ("gpt.ckpt" if is_gpt else "sovits.pth")
        dst = os.path.join(dest, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            with open(dst, "wb") as f:
                f.write(zf.read(n))
        except Exception:
            continue
        if is_gpt:
            gpt_rel = dst_rel
        else:
            sovits_rel = dst_rel

    # 2) 立绘列表：本项目包用自己的 sprites（保留情绪标签/绑定音频）；否则按文件名推断标签
    sprites = []
    if isinstance(meta.get("sprites"), list) and meta["sprites"]:
        for it in meta["sprites"]:
            if isinstance(it, dict) and str(it.get("path") or "").strip():
                sprites.append(it)
    if not sprites:
        sprites = [{"path": rel, "emotion_tags": _tags_from_filename(rel), "voice_path": ""}
                   for rel in copied]

    profile = str(meta.get("profile") or "").strip()
    if not profile:
        brief = str(meta.get("character_brief") or "").strip()
        setting = str(meta.get("character_setting") or "").strip()
        profile = "\n".join([x for x in (brief, setting) if x])

    # 配音预设：本项目包沿用自带预设；Shinsekai 包按「参考音频 / 微调权重」建预设
    tts = meta.get("tts")
    if not (isinstance(tts, dict) and tts.get("presets")):
        tts = None
        if ref_rel or gpt_rel or sovits_rel:
            tts = {"active": "角色音色", "presets": {"角色音色": {
                "provider": "gpt-sovits", "voice": "", "model": "", "speed": 1.0, "url": "",
                "ref_audio": ref_rel,
                "prompt_text": str(meta.get("prompt_text") or "").strip(),
                "prompt_lang": str(meta.get("prompt_lang") or "").strip().lower(),
                "gpt_weights": gpt_rel,
                "sovits_weights": sovits_rel,
            }}}

    char = save_character(cid, {
        "name": name,
        "color": meta.get("color"),
        "sprite_scale": meta.get("sprite_scale"),
        "profile": profile,
        "sprites": sprites,
        "tts": tts,
    })
    return {"character": char, "sprite_files": len(copied), "ref_audio": ref_rel,
            "speech_files": speech_files,
            "gpt_weights": gpt_rel, "sovits_weights": sovits_rel,
            "skipped_voice_model": False}


def list_packs():
    """data/gal/packs/ 下待导入的角色包（.char / .zip）"""
    os.makedirs(PACKS_DIR, exist_ok=True)
    return sorted(f for f in os.listdir(PACKS_DIR)
                  if f.lower().endswith((".char", ".zip"))
                  and os.path.isfile(os.path.join(PACKS_DIR, f)))


def import_packs_dir():
    """批量导入 data/gal/packs/ 下的所有角色包"""
    ok, ids, failed = 0, [], []
    for fn in list_packs():
        try:
            r = import_character_pack(filename=fn, path=os.path.join(PACKS_DIR, fn))
            ok += 1
            ids.append(r["character"]["id"])
        except Exception as e:
            failed.append("%s：%s" % (fn, e))
    return {"imported": ok, "ids": ids, "failed": failed}



def resolve_asset(cid, rel):
    """把角色目录内的相对路径解析成绝对路径（带越界与类型校验；图片或音频）"""
    base = char_dir(cid)
    rb = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, str(rel or "").replace("\\", "/").strip()))
    if target != rb and not target.startswith(rb + os.sep):
        raise ValueError("资源路径越界")
    ext = os.path.splitext(target)[1].lower()
    if ext not in MIME_EXT:
        raise ValueError("不支持的资源类型")
    if not os.path.isfile(target):
        raise ValueError("资源文件不存在：" + str(rel))
    if os.path.getsize(target) > MAX_ASSET_BYTES:
        raise ValueError("资源文件过大")
    return target, MIME_EXT[ext]


def resolve_weight(cid, rel):
    """解析角色目录内的 GPT-SoVITS 微调权重（.ckpt / .pth）绝对路径"""
    base = char_dir(cid)
    rb = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, str(rel or "").replace("\\", "/").strip()))
    if target != rb and not target.startswith(rb + os.sep):
        raise ValueError("资源路径越界")
    if os.path.splitext(target)[1].lower() not in WEIGHT_EXT:
        raise ValueError("不是 GPT-SoVITS 权重文件（需 .ckpt / .pth）")
    if not os.path.isfile(target):
        raise ValueError("权重文件不存在：" + str(rel))
    return target


def resolve_media(kind, rel):
    """解析背景（backgrounds/）或 BGM（bgm/）资源"""
    base = BACKGROUND_DIR if kind == "bg" else BGM_DIR
    allow = MIME_EXT if kind == "bg" else AUDIO_EXT
    os.makedirs(base, exist_ok=True)
    rb = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, str(rel or "").replace("\\", "/").strip()))
    if target != rb and not target.startswith(rb + os.sep):
        raise ValueError("资源路径越界")
    ext = os.path.splitext(target)[1].lower()
    if ext not in allow:
        raise ValueError("不支持的资源类型")
    if not os.path.isfile(target):
        raise ValueError("资源文件不存在：" + str(rel))
    if os.path.getsize(target) > MAX_ASSET_BYTES:
        raise ValueError("资源文件过大")
    return target, allow[ext]


def list_sprite_files(cid):
    """列出角色目录下的图片/音频文件（相对路径），供编辑器选择"""
    base = char_dir(cid)
    out = []
    for dirpath, _dirs, files in os.walk(base):
        for fn in sorted(files):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in MIME_EXT:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), base).replace("\\", "/")
            out.append(rel)
    return sorted(out)


# ============================================================
# 1b. 背景库（图片在 backgrounds/，名称与分类标签记在 backgrounds.json）
# ============================================================
def _safe_bg_rel(rel):
    """把背景相对路径收敛成安全形式（只允许 backgrounds/ 内的相对路径）"""
    rel = str(rel or "").replace("\\", "/").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        return ""
    return rel


def _bg_abs(rel):
    rel = _safe_bg_rel(rel)
    if not rel:
        return ""
    return os.path.join(BACKGROUND_DIR, *rel.split("/"))


def _bg_display_name(rel):
    """背景的显示名（只读索引，不触发扫描/回写；找不到就用文件名）"""
    rel = _safe_bg_rel(rel)
    if not rel:
        return ""
    name = ""
    try:
        with open(BACKGROUND_INDEX, "r", encoding="utf-8") as f:
            for b in (json.load(f) or {}).get("items") or []:
                if _safe_bg_rel((b or {}).get("file")) == rel:
                    name = str((b or {}).get("name") or "").strip()
                    break
    except Exception:
        name = ""
    return name or os.path.splitext(os.path.basename(rel))[0]


def _norm_tags(raw, dims=BG_TAG_DIMS):
    """把标签收敛成 {维度: [...], "extra": [自由标签]}

    兼容三种输入：字符串 / 字符串数组 / 分类字典（{"场景": [...], "extra": [...]}）
    dims 指定固定维度（背景用 BG_TAG_DIMS，BGM 用 BGM_TAG_DIMS）
    """
    out = {d: [] for d in dims}
    extra = []

    def _add(bucket, v):
        for t in ([v] if isinstance(v, str) else (v or [])):
            t = str(t).strip()
            if t and t not in bucket:
                bucket.append(t)

    if isinstance(raw, dict):
        for d in dims:
            if d in raw:
                _add(out[d], raw.get(d))
        _add(extra, raw.get("extra") or raw.get("其他") or raw.get("标签"))
    else:
        _add(extra, raw)
    out["extra"] = extra
    return out


def normalize_background(it):
    if not isinstance(it, dict):
        return None
    rel = _safe_bg_rel(it.get("file"))
    if not rel:
        return None
    return {
        "file": rel,
        "name": str(it.get("name") or "").strip()[:60],
        "tags": _norm_tags(it.get("tags")),
    }


# 文件名自动归类用的关键词（够用即可，用户可在 UI 里手改）
_BG_DIM_HINTS = {
    "场景": ("教室", "海边", "河边", "街道", "走廊", "天台", "屋顶", "公园", "森林", "房间",
             "卧室", "客厅", "厨房", "餐厅", "咖啡", "神社", "车站", "图书馆", "医院", "学校",
             "家", "城市", "广场", "桥", "山路", "湖边", "海边", "沙滩", "星空", "樱花", "庭院"),
    "时间": ("清晨", "早晨", "上午", "中午", "午后", "下午", "黄昏", "傍晚", "夜晚", "深夜",
             "凌晨", "白天", "日落", "日出", "黎明", "夜"),
    "天气": ("晴朗", "多云", "小雨", "大雨", "下雨", "下雪", "雾", "彩虹", "晴", "阴", "雨", "雪", "风"),
    "氛围": ("温馨", "安静", "热闹", "悲伤", "浪漫", "紧张", "恐怖", "梦幻", "孤独", "宁静", "温暖", "冷清"),
}


def _infer_tags_from_name(rel):
    """从背景文件名推断分类标签（分类关键词命中维度，其余进 extra）"""
    tags = {d: [] for d in BG_TAG_DIMS}
    extra = []
    for w in _tags_from_filename(rel):
        placed = False
        for d, kws in _BG_DIM_HINTS.items():
            if any((k in w) or (w in k) for k in kws):
                if w not in tags[d]:
                    tags[d].append(w)
                placed = True
                break
        if not placed and w not in extra:
            extra.append(w)
    tags["extra"] = extra
    return tags


def _scan_bg_files():
    """扫描 backgrounds/ 下所有图片，返回相对路径（正斜杠）列表"""
    if not os.path.isdir(BACKGROUND_DIR):
        return []
    out = []
    for dirpath, _dirs, files in os.walk(BACKGROUND_DIR):
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() not in IMAGE_EXT:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), BACKGROUND_DIR).replace("\\", "/")
            out.append(rel)
    return sorted(out)


def save_backgrounds(data):
    os.makedirs(GAL_DIR, exist_ok=True)
    tmp = BACKGROUND_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data or {"items": []}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BACKGROUND_INDEX)


def load_backgrounds():
    """读取背景库 {items:[{file,name,tags}]}

    - 目录里有、索引里没有的图片：自动补一条（名称=文件名，标签自动推断）
    - 索引里有、文件已删除的条目：自动剔除
    变化时回写 backgrounds.json。
    """
    os.makedirs(BACKGROUND_DIR, exist_ok=True)
    raw = {}
    if os.path.isfile(BACKGROUND_INDEX):
        try:
            with open(BACKGROUND_INDEX, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    items = [b for b in (normalize_background(x) for x in ((raw or {}).get("items") or [])) if b]
    have = {b["file"] for b in items}
    exists = set(_scan_bg_files())
    changed = False
    for rel in exists:
        if rel not in have:
            items.append({"file": rel, "name": os.path.splitext(os.path.basename(rel))[0],
                          "tags": _infer_tags_from_name(rel)})
            changed = True
    kept = []
    for b in items:
        if b["file"] in exists:
            kept.append(b)
        else:
            changed = True
    result = {"items": kept}
    if changed or not os.path.isfile(BACKGROUND_INDEX):
        save_backgrounds(result)
    return result


def background_roster():
    """给提示词用的背景清单文本（按维度分组）+ 名称列表"""
    items = load_backgrounds()["items"]
    lines, names = [], []
    for i, b in enumerate(items):
        nm = b.get("name") or os.path.splitext(os.path.basename(b["file"]))[0]
        names.append(nm)
        tg = b.get("tags") or {}
        parts = [d + "：" + "、".join(tg[d]) for d in BG_TAG_DIMS if tg.get(d)]
        if tg.get("extra"):
            parts.append("标签：" + "、".join(tg["extra"]))
        lines.append("%d. %s%s" % (i + 1, nm, ("（" + "；".join(parts) + "）") if parts else ""))
    return "\n".join(lines), names


def match_background(spec):
    """把 AI 给的背景编号 / 文件名 / 名称 / 标签匹配到条目；匹配不到返回 None"""
    items = load_backgrounds()["items"]
    if not items:
        return None
    if isinstance(spec, int) and 1 <= spec <= len(items):
        return items[spec - 1]
    keys = spec if isinstance(spec, list) else ([spec] if spec else [])
    keys = [str(k or "").strip() for k in keys]
    keys = [k for k in keys if k]
    if not keys:
        return None
    # 1) 文件名 / 名称精确 + 包含
    for k in keys:
        for b in items:
            if k == b["file"] or k == b.get("name"):
                return b
    for k in keys:
        kl = k.lower()
        for b in items:
            if kl in os.path.basename(b["file"]).lower() or k in (b.get("name") or ""):
                return b
    # 2) 标签模糊匹配
    for k in keys:
        for b in items:
            tg = b.get("tags") or {}
            pool = [t for d in BG_TAG_DIMS for t in (tg.get(d) or [])] + (tg.get("extra") or [])
            for t in pool:
                if k == t or k in t or t in k:
                    return b
    return None


def resolve_background(spec):
    """把 AI 的 bg 值解析成真实文件相对路径；解析不到返回 ""

    兼容旧写法：如果给的本来就是存在的文件路径，原样返回。
    """
    s = str(spec or "").replace("\\", "/").strip()
    if s:
        p = _bg_abs(s)
        if p and os.path.isfile(p):
            return _safe_bg_rel(s)
    hit = match_background(spec)
    return hit["file"] if hit else ""


def save_background_upload(filename, b64, name="", tags=None):
    """上传一张背景图：落盘 + 写入索引；返回新条目"""
    try:
        raw = base64.b64decode(b64 or "")
    except Exception:
        raise ValueError("图片内容解析失败")
    if not raw:
        raise ValueError("图片内容为空")
    if len(raw) > MAX_ASSET_BYTES:
        raise ValueError("图片过大（上限 %d MB）" % (MAX_ASSET_BYTES // (1024 * 1024)))
    ext = os.path.splitext(str(filename or ""))[1].lower()
    if ext not in IMAGE_EXT:
        raise ValueError("不支持的图片格式（png / jpg / gif / webp / bmp）")
    os.makedirs(BACKGROUND_DIR, exist_ok=True)
    data = load_backgrounds()          # 先读索引：此时新文件尚未落盘，避免被自动扫描重复收录
    base = re.sub(r"[^\w.-]", "_",
                  os.path.splitext(os.path.basename(str(filename or "bg")))[0]).strip("_")[:48] or "bg"
    fname, i = base + ext, 2
    while os.path.exists(os.path.join(BACKGROUND_DIR, fname)):
        fname = "%s-%d%s" % (base, i, ext)
        i += 1
    with open(os.path.join(BACKGROUND_DIR, fname), "wb") as f:
        f.write(raw)
    item = {"file": fname,
            "name": (str(name or "").strip()[:60] or os.path.splitext(fname)[0]),
            "tags": _norm_tags(tags) if tags is not None else _infer_tags_from_name(fname)}
    data["items"].append(item)
    save_backgrounds(data)
    return item


def update_background(file, name=None, tags=None):
    data = load_backgrounds()
    rel = _safe_bg_rel(file)
    for b in data["items"]:
        if b["file"] == rel:
            if name is not None:
                b["name"] = str(name).strip()[:60]
            if tags is not None:
                b["tags"] = _norm_tags(tags)
            save_backgrounds(data)
            return b
    raise ValueError("背景不存在")


def delete_background(file):
    """删除背景：从索引移除并删除文件"""
    data = load_backgrounds()
    rel = _safe_bg_rel(file)
    hit = None
    keep = []
    for b in data["items"]:
        if b["file"] == rel:
            hit = b
        else:
            keep.append(b)
    if not hit:
        raise ValueError("背景不存在")
    data["items"] = keep
    save_backgrounds(data)
    p = _bg_abs(rel)
    if p and os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass
    return True


# ============================================================
# 1b-2. BGM 库（音频仍在 bgm/ 下，名称与标签记在 bgm.json）
#       与「背景库」同构：扫描自动收录、AI 按名称/标签匹配
# ============================================================
def _safe_bgm_rel(rel):
    """把 BGM 相对路径收敛成安全形式（只允许 bgm/ 内的相对路径）"""
    rel = str(rel or "").replace("\\", "/").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        return ""
    return rel


def _bgm_abs(rel):
    rel = _safe_bgm_rel(rel)
    if not rel:
        return ""
    return os.path.join(BGM_DIR, *rel.split("/"))


def _bgm_display_name(rel):
    """BGM 的显示名（只读索引，不触发扫描/回写；找不到就用文件名）"""
    rel = _safe_bgm_rel(rel)
    if not rel:
        return ""
    name = ""
    try:
        with open(BGM_INDEX, "r", encoding="utf-8") as f:
            for b in (json.load(f) or {}).get("items") or []:
                if _safe_bgm_rel((b or {}).get("file")) == rel:
                    name = str((b or {}).get("name") or "").strip()
                    break
    except Exception:
        name = ""
    return name or os.path.splitext(os.path.basename(rel))[0]


def normalize_bgm(it):
    if not isinstance(it, dict):
        return None
    rel = _safe_bgm_rel(it.get("file"))
    if not rel:
        return None
    return {
        "file": rel,
        "name": str(it.get("name") or "").strip()[:60],
        "tags": _norm_tags(it.get("tags"), BGM_TAG_DIMS),
    }


# BGM 文件名自动归类用的关键词（够用即可，用户可在 UI 里手改）
_BGM_DIM_HINTS = {
    "情绪": ("紧张", "悲伤", "忧伤", "温馨", "温柔", "热血", "激昂", "恐怖", "惊悚", "悬疑",
             "孤独", "宁静", "欢快", "活泼", "压抑", "绝望", "希望", "感动", "恢弘", "神秘", "悲伤"),
    "场景": ("日常", "战斗", "探索", "相遇", "离别", "结局", "追逐", "城镇", "夜晚", "清晨",
             "回忆", "序章", "高潮", "告白", "抉择", "冒险", "逃亡"),
    "氛围": ("安静", "诡异", "悲壮", "浪漫", "梦幻", "肃杀", "温暖", "荒凉", "轻松", "沉重"),
}


def _infer_bgm_tags_from_name(rel):
    """从 BGM 文件名推断分类标签（分类关键词命中维度，其余进 extra）"""
    tags = {d: [] for d in BGM_TAG_DIMS}
    extra = []
    for w in _tags_from_filename(rel):
        placed = False
        for d, kws in _BGM_DIM_HINTS.items():
            if any((k in w) or (w.lower() in k.lower()) for k in kws):
                if w not in tags[d]:
                    tags[d].append(w)
                placed = True
                break
        if not placed and w not in extra:
            extra.append(w)
    tags["extra"] = extra
    return tags


def _scan_bgm_files():
    """扫描 bgm/ 下所有音频，返回相对路径（正斜杠）列表"""
    if not os.path.isdir(BGM_DIR):
        return []
    out = []
    for dirpath, _dirs, files in os.walk(BGM_DIR):
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() not in AUDIO_EXT:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), BGM_DIR).replace("\\", "/")
            out.append(rel)
    return sorted(out)


def save_bgms(data):
    os.makedirs(GAL_DIR, exist_ok=True)
    tmp = BGM_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data or {"items": []}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BGM_INDEX)


def load_bgms():
    """读取 BGM 库 {items:[{file,name,tags}]}

    - 目录里有、索引里没有的音频：自动补一条（名称=文件名，标签自动推断）
    - 索引里有、文件已删除的条目：自动剔除
    变化时回写 bgm.json。
    """
    os.makedirs(BGM_DIR, exist_ok=True)
    raw = {}
    if os.path.isfile(BGM_INDEX):
        try:
            with open(BGM_INDEX, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    items = [b for b in (normalize_bgm(x) for x in ((raw or {}).get("items") or [])) if b]
    have = {b["file"] for b in items}
    exists = set(_scan_bgm_files())
    changed = False
    for rel in exists:
        if rel not in have:
            items.append({"file": rel, "name": os.path.splitext(os.path.basename(rel))[0],
                          "tags": _infer_bgm_tags_from_name(rel)})
            changed = True
    kept = []
    for b in items:
        if b["file"] in exists:
            kept.append(b)
        else:
            changed = True
    result = {"items": kept}
    if changed or not os.path.isfile(BGM_INDEX):
        save_bgms(result)
    return result


def bgm_roster():
    """给提示词用的 BGM 清单文本（按维度分组）+ 名称列表"""
    items = load_bgms()["items"]
    lines, names = [], []
    for i, b in enumerate(items):
        nm = b.get("name") or os.path.splitext(os.path.basename(b["file"]))[0]
        names.append(nm)
        tg = b.get("tags") or {}
        parts = [d + "：" + "、".join(tg[d]) for d in BGM_TAG_DIMS if tg.get(d)]
        if tg.get("extra"):
            parts.append("标签：" + "、".join(tg["extra"]))
        lines.append("%d. %s%s" % (i + 1, nm, ("（" + "；".join(parts) + "）") if parts else ""))
    return "\n".join(lines), names


def match_bgm(spec):
    """把 AI 给的 BGM 编号 / 文件名 / 名称 / 标签匹配到条目；匹配不到返回 None"""
    items = load_bgms()["items"]
    if not items:
        return None
    if isinstance(spec, int) and 1 <= spec <= len(items):
        return items[spec - 1]
    keys = spec if isinstance(spec, list) else ([spec] if spec else [])
    keys = [str(k or "").strip() for k in keys]
    keys = [k for k in keys if k]
    if not keys:
        return None
    # 1) 文件名 / 名称精确 + 包含
    for k in keys:
        for b in items:
            if k == b["file"] or k == b.get("name"):
                return b
    for k in keys:
        kl = k.lower()
        for b in items:
            if kl in os.path.basename(b["file"]).lower() or k in (b.get("name") or ""):
                return b
    # 2) 标签模糊匹配
    for k in keys:
        for b in items:
            tg = b.get("tags") or {}
            pool = [t for d in BGM_TAG_DIMS for t in (tg.get(d) or [])] + (tg.get("extra") or [])
            for t in pool:
                if k == t or k in t or t in k:
                    return b
    return None


def resolve_bgm(spec):
    """把 AI 的 bgm 值解析成真实文件相对路径；解析不到返回 ""

    兼容旧写法：如果给的本来就是存在的文件路径，原样返回。
    """
    s = str(spec or "").replace("\\", "/").strip()
    if s:
        p = _bgm_abs(s)
        if p and os.path.isfile(p):
            return _safe_bgm_rel(s)
    hit = match_bgm(spec)
    return hit["file"] if hit else ""


def is_bgm_stop(spec):
    """判断 AI 的 bgm 值是否是「停止播放」指令（空串不算，空串=不改变）"""
    s = str(spec or "").strip().lower()
    return s in ("stop", "none", "off", "silence", "mute", "停", "停止", "无", "静音", "安静")


def save_bgm_upload(filename, b64, name="", tags=None):
    """上传一首 BGM：落盘 + 写入索引；返回新条目"""
    try:
        raw = base64.b64decode(b64 or "")
    except Exception:
        raise ValueError("音频内容解析失败")
    if not raw:
        raise ValueError("音频内容为空")
    if len(raw) > MAX_ASSET_BYTES:
        raise ValueError("音频过大（上限 %d MB）" % (MAX_ASSET_BYTES // (1024 * 1024)))
    ext = os.path.splitext(str(filename or ""))[1].lower()
    if ext not in AUDIO_EXT:
        raise ValueError("不支持的音频格式（mp3 / wav / ogg / m4a / flac）")
    os.makedirs(BGM_DIR, exist_ok=True)
    data = load_bgms()                 # 先读索引：此时新文件尚未落盘，避免被自动扫描重复收录
    base = re.sub(r"[^\w.-]", "_",
                  os.path.splitext(os.path.basename(str(filename or "bgm")))[0]).strip("_")[:48] or "bgm"
    fname, i = base + ext, 2
    while os.path.exists(os.path.join(BGM_DIR, fname)):
        fname = "%s-%d%s" % (base, i, ext)
        i += 1
    with open(os.path.join(BGM_DIR, fname), "wb") as f:
        f.write(raw)
    item = {"file": fname,
            "name": (str(name or "").strip()[:60] or os.path.splitext(fname)[0]),
            "tags": (_norm_tags(tags, BGM_TAG_DIMS) if tags is not None
                     else _infer_bgm_tags_from_name(fname))}
    data["items"].append(item)
    save_bgms(data)
    return item


def update_bgm(file, name=None, tags=None):
    data = load_bgms()
    rel = _safe_bgm_rel(file)
    for b in data["items"]:
        if b["file"] == rel:
            if name is not None:
                b["name"] = str(name).strip()[:60]
            if tags is not None:
                b["tags"] = _norm_tags(tags, BGM_TAG_DIMS)
            save_bgms(data)
            return b
    raise ValueError("BGM 不存在")


def delete_bgm(file):
    """删除 BGM：从索引移除并删除文件"""
    data = load_bgms()
    rel = _safe_bgm_rel(file)
    hit = None
    keep = []
    for b in data["items"]:
        if b["file"] == rel:
            hit = b
        else:
            keep.append(b)
    if not hit:
        raise ValueError("BGM 不存在")
    data["items"] = keep
    save_bgms(data)
    p = _bgm_abs(rel)
    if p and os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass
    return True


# ============================================================
# 1c. 舞台状态参数（stats）：AI 按角色设定自建的「临时数值」
#     存于会话（s["gal_stats"]），不进知识库；纯展示用途，无副作用
# ============================================================
STAT_MAX = 8            # 单次最多几个参数
STAT_KEY_MAX = 16       # key 最大长度
STAT_LABEL_MAX = 8      # 显示名最大长度
STAT_TEXT_MAX = 20      # 文本型数值最大长度
DEFAULT_STAT_ICON = "●"

# 图标名 → 表情（AI 可填名字，也可直接写一个表情）
_STAT_ICONS = {
    "heart": "❤", "love": "❤", "好感": "❤", "好感度": "❤",
    "time": "🕐", "clock": "🕐", "时间": "🕐",
    "food": "🍖", "hunger": "🍖", "饥饿": "🍖", "饱食": "🍖",
    "water": "💧", "水分": "💧", "口渴": "💧",
    "supply": "📦", "supplies": "📦", "物资": "📦",
    "hp": "❤️🩹", "health": "❤️🩹", "生命": "❤️🩹",
    "mp": "✨", "mana": "✨", "魔力": "✨",
    "money": "💰", "gold": "💰", "金钱": "💰",
    "stamina": "⚡", "体力": "⚡",
    "temp": "🌡", "temperature": "🌡", "温度": "🌡",
    "weather": "☁", "天气": "☁",
    "mood": "🎭", "心情": "🎭",
    "level": "⭐", "等级": "⭐",
    "rep": "🏅", "reputation": "🏅", "声望": "🏅",
    "morale": "🔥", "士气": "🔥",
    "danger": "⚠", "危险": "⚠",
}
STAT_ICON_NAMES = ("heart", "time", "food", "water", "supply", "hp", "mp",
                   "money", "stamina", "temp", "weather", "mood", "level", "rep",
                   "morale", "danger")


def _stat_icon(v):
    s = str(v or "").strip()
    if not s:
        return DEFAULT_STAT_ICON
    return _STAT_ICONS.get(s) or _STAT_ICONS.get(s.lower()) or s[:4]


def _stat_num(v):
    """尽量把值转成整数；转不了返回 None（不把 bool 当数字）"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(round(v))
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def _clamp_stat(it):
    """数字型数值按 min/max 夹取（文本型不动）；只给 max 时下限默认 0（进度条从 0 起）"""
    v = it.get("value")
    if isinstance(v, int):
        lo, hi = it.get("min"), it.get("max")
        if lo is None and hi is not None:
            lo = 0
        if isinstance(lo, int):
            v = max(lo, v)
        if isinstance(hi, int):
            v = min(hi, v)
        it["value"] = v
    return it


def normalize_stat(it):
    """把 AI 给的一个参数收敛成 {key,label,icon,value,min,max,delta?}；无效返回 None"""
    if not isinstance(it, dict):
        return None
    key = str(it.get("key") or it.get("id") or it.get("name") or it.get("label") or "").strip()
    key = key[:STAT_KEY_MAX]
    if not key:
        return None
    out = {"key": key, "label": str(it.get("label") or it.get("name") or key).strip()[:STAT_LABEL_MAX] or key}
    if "icon" in it:
        out["icon"] = _stat_icon(it.get("icon"))
    val = it.get("value")
    if isinstance(val, str):
        s = val.strip()
        if s:
            out["value"] = _stat_num(s) if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s) else s[:STAT_TEXT_MAX]
    else:
        n = _stat_num(val)
        if n is not None:
            out["value"] = n
    mn, mx = _stat_num(it.get("min")), _stat_num(it.get("max"))
    if mn is not None:
        out["min"] = mn
    if mx is not None:
        out["max"] = mx
    d = _stat_num(it.get("delta"))
    if d is not None:
        out["delta"] = d
    return _clamp_stat(out)


def _norm_stats(raw):
    """AI 回复里的 stats 字段 → 归一化列表（容忍 dict 包裹 / 单对象）"""
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("stats") or [raw]
    if not isinstance(raw, list):
        return []
    return [x for x in (normalize_stat(i) for i in raw) if x][:STAT_MAX]


def stats_from_messages(msgs):
    """回滚用：取最后一条带 gal.stats_after 的消息所记录的状态参数。

    撤回 / 重新发言 / 清空会话后调用，让数值跟着对话一起后退。
    """
    for m in reversed(msgs or []):
        if not isinstance(m, dict):
            continue
        g = m.get("gal")
        if isinstance(g, dict) and "stats_after" in g:
            return g.get("stats_after") or []
    return []


def _merge_stat(dst, src):
    """把 src 的值并进 dst（同一条参数被重复声明时：后者覆盖前者）"""
    for k in ("label", "icon", "min", "max"):
        if src.get(k) is not None:
            dst[k] = src[k]
    if src.get("value") is not None:
        dst["value"] = src["value"]
    return _clamp_stat(dst)


def apply_stats(current, incoming):
    """把 AI 给的 stats 合并进会话状态；返回 (items, changed)

    - 合并口径：**同一个 key 或同一个 label 视为同一条**。
      模型常把「生命力」写成 key=生命 / key=生命力 两种写法，只按 key 去重会攒出两条。
    - current 自身的重复也会在这一步合并掉（列表靠后的覆盖靠前的）——攒下的旧数据能自愈。
    - 新 key：补建（只给 delta 时以 min 为基准累加）
    - 已有：更新 label/icon/min/max；delta 累加、value 覆盖（数字型才累加）
    - 全过程按 min/max 夹取，最终最多 STAT_MAX 条
    """
    items = []
    by_key, by_label = {}, {}
    changed = False

    def _find(d):
        return by_key.get(d["key"]) or by_label.get(d.get("label") or d["key"])

    def _index(d):
        by_key[d["key"]] = d
        by_label[d.get("label") or d["key"]] = d

    # 1) 并入 current，顺带合掉它自身的重复
    for raw in (normalize_stat(i) for i in (current or [])):
        if not raw:
            continue
        cur = _find(raw)
        if cur is None:
            items.append(raw)
            _index(raw)
            continue
        before = json.dumps(cur, ensure_ascii=False, sort_keys=True)
        _merge_stat(cur, raw)
        if json.dumps(cur, ensure_ascii=False, sort_keys=True) != before:
            changed = True
        _index(cur)            # 两种写法都指向同一条，后续 incoming 用哪种都能命中

    # 2) 并入 incoming
    for inc in _norm_stats(incoming):
        cur = _find(inc)
        if cur is None:
            new = {"key": inc["key"], "label": inc["label"], "icon": inc.get("icon", DEFAULT_STAT_ICON)}
            for k in ("min", "max"):
                if k in inc:
                    new[k] = inc[k]
            if inc.get("value") is not None:
                new["value"] = inc["value"]
            elif inc.get("delta") is not None:
                base = inc.get("min", 0) if isinstance(inc.get("min"), int) else 0
                new["value"] = base + inc["delta"]
            else:
                continue                     # 只有声明没有值 → 跳过，别建空条
            items.append(_clamp_stat(new))
            _index(new)
            changed = True
            continue
        before = json.dumps(cur, ensure_ascii=False, sort_keys=True)
        if inc.get("label"):
            cur["label"] = inc["label"]
        if "icon" in inc:
            cur["icon"] = inc["icon"]
        for k in ("min", "max"):
            if k in inc:
                cur[k] = inc[k]
        if inc.get("delta") is not None and isinstance(cur.get("value"), int):
            cur["value"] = cur["value"] + inc["delta"]
        elif inc.get("value") is not None:
            cur["value"] = inc["value"]
        _clamp_stat(cur)
        _index(cur)
        if json.dumps(cur, ensure_ascii=False, sort_keys=True) != before:
            changed = True
    return items[:STAT_MAX], changed


def save_upload(cid, filename, b64):
    """把 base64 图片/音频写入角色目录的 sprites/，返回相对路径"""
    import base64 as _b64
    base = char_dir(cid)
    ext = os.path.splitext(str(filename or ""))[1].lower()
    if ext not in MIME_EXT:
        raise ValueError("不支持的文件类型")
    try:
        raw = _b64.b64decode(b64 or "")
    except Exception:
        raise ValueError("文件内容解析失败")
    if not raw or len(raw) > MAX_ASSET_BYTES:
        raise ValueError("文件为空或过大")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(os.path.basename(str(filename)))[0])[:48] or "file"
    rel = "sprites/" + name + ext
    full = os.path.join(base, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(raw)
    return rel


# ============================================================
# 关键词特效（*闪光* / 【震动】 等 → 画面 / 音效）
# ============================================================
EFFECTS_DIR = os.path.join(GAL_DIR, "effects")
EFFECTS_FILE = os.path.join(EFFECTS_DIR, "effects.json")
EFFECT_CSS = ("flash", "shake", "blackout", "fade")
MAP_POS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北", "中")
# 九宫格方位 → 平面俯视图坐标（0–1，0,0 = 西北角）；仅用于兼容旧的 `pos` 写法
MAP_POS_XY = {
    "西北": (0.17, 0.17), "北": (0.50, 0.14), "东北": (0.83, 0.17),
    "西": (0.14, 0.50), "中": (0.50, 0.50), "东": (0.86, 0.50),
    "西南": (0.17, 0.83), "南": (0.50, 0.86), "东南": (0.83, 0.83),
}
DEFAULT_EFFECTS = [
    {"keyword": "闪光", "css": "flash", "audio": "", "mode": "after"},
    {"keyword": "震动", "css": "shake", "audio": "", "mode": "after"},
    {"keyword": "黑屏", "css": "blackout", "audio": "", "mode": "after"},
    {"keyword": "淡入", "css": "fade", "audio": "", "mode": "after"},
]
_FX_RE = re.compile(r"\*([^*\n]{1,16})\*|【([^】\n]{1,16})】")


def _normalize_effect(it):
    if not isinstance(it, dict):
        return None
    kw = str(it.get("keyword") or "").strip()[:16]
    if not kw:
        return None
    css = str(it.get("css") or "").strip().lower()
    mode = str(it.get("mode") or "after").strip().lower()
    return {
        "keyword": kw,
        "css": css if css in EFFECT_CSS else "",
        "audio": str(it.get("audio") or "").replace("\\", "/").strip(),
        "mode": mode if mode in ("before", "after", "loop", "stop") else "after",
    }


def load_effects():
    """关键词特效表（data/gal/effects/effects.json；缺失则用内置默认）"""
    if not os.path.isfile(EFFECTS_FILE):
        return [dict(x) for x in DEFAULT_EFFECTS]
    try:
        with open(EFFECTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return [dict(x) for x in DEFAULT_EFFECTS]
    items = data.get("effects") if isinstance(data, dict) else data
    out = [e for e in (_normalize_effect(x) for x in (items or [])) if e]
    return out or [dict(x) for x in DEFAULT_EFFECTS]


def effect_catalog_text(effects):
    """给提示词的「可用特效」清单"""
    if not effects:
        return ""
    return ("可用舞台特效：在 `speech` 里用 `*关键词*` 触发（会被渲染成画面/音效，且不会被朗读）："
            + "、".join(e["keyword"] for e in effects) + "。")


def extract_effects(text, effects):
    """从台词里抽出特效关键词并移除；返回 (清理后的文本, [fx, ...])"""
    src = str(text or "")
    if not src or not effects:
        return src, []
    table = {e["keyword"]: e for e in effects}
    hits = []

    def _rep(m):
        kw = (m.group(1) or m.group(2) or "").strip()
        e = table.get(kw)
        if not e:
            return m.group(0)          # 未注册 → 原样保留（当作普通强调）
        hits.append({"keyword": kw, "css": e["css"], "audio": e["audio"], "mode": e["mode"]})
        return ""

    return _FX_RE.sub(_rep, src).strip(), hits[:4]


def resolve_effect_audio(rel):
    """特效音频字节（data/gal/effects/ 下的文件）"""
    base = EFFECTS_DIR
    os.makedirs(base, exist_ok=True)
    rb = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, str(rel or "").replace("\\", "/").strip()))
    if target != rb and not target.startswith(rb + os.sep):
        raise ValueError("资源路径越界")
    ext = os.path.splitext(target)[1].lower()
    if ext not in AUDIO_EXT:
        raise ValueError("不支持的音频类型")
    if not os.path.isfile(target):
        raise ValueError("音频不存在：" + str(rel))
    if os.path.getsize(target) > MAX_ASSET_BYTES:
        raise ValueError("音频过大")
    return target, AUDIO_EXT[ext]


# ============================================================
# 剧本（轻量剧情节点）
# ============================================================
STORY_NAME = "story.json"
NODE_TYPES = ("limited_turn", "free_chat", "ending")


def load_story(cid):
    p = os.path.join(char_dir(cid), STORY_NAME)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return normalize_story(data)


def normalize_story(data):
    data = data if isinstance(data, dict) else {}
    nodes = []
    for it in (data.get("nodes") or []):
        if not isinstance(it, dict):
            continue
        nid = str(it.get("id") or "").strip()
        if not nid:
            continue
        ntype = str(it.get("type") or "free_chat")
        if ntype not in NODE_TYPES:
            ntype = "free_chat"
        trans = [str(x).strip() for x in (it.get("transitions") or []) if str(x).strip()]
        try:
            mr = int(it.get("maxRounds") or 0)
        except (TypeError, ValueError):
            mr = 0
        nodes.append({"id": nid, "type": ntype, "maxRounds": max(0, mr),
                      "transitions": trans,
                      "defaultTo": str(it.get("defaultTo") or "").strip(),
                      "prompt": str(it.get("prompt") or "")})
    ids = {n["id"] for n in nodes}
    start = str(data.get("start") or "").strip()
    if start not in ids:
        start = nodes[0]["id"] if nodes else ""
    return {"start": start, "nodes": nodes}


def save_story(cid, data):
    d = char_dir(cid)
    os.makedirs(d, exist_ok=True)
    story = normalize_story(data)
    with open(os.path.join(d, STORY_NAME), "w", encoding="utf-8") as f:
        json.dump(story, f, ensure_ascii=False, indent=2)
    return story


def story_node(story, node_id):
    if not story:
        return None
    for n in story.get("nodes") or []:
        if n["id"] == node_id:
            return n
    return None


def build_node_prompt(node, round_no):
    if not node:
        return ""
    parts = [P.part("node", "head", id=node["id"], type=node["type"])]
    if node.get("prompt"):
        parts.append(node["prompt"])
    if node["type"] == "limited_turn":
        parts.append(P.part("node", "limit", round_no=round_no,
                            maxRounds=node["maxRounds"]))
    elif node["type"] == "ending":
        parts.append(P.part("node", "ending"))
    parts.append(P.part("node", "tail"))
    return "\n".join(parts)


def build_choice_prompt(node):
    """让模型选择下一个节点（只返回一个 JSON）"""
    opts = list(node.get("transitions") or [])
    return P.part("choice", "head", id=node["id"],
                  opts=("、".join(opts) if opts else "（无）"))


def parse_node_choice(text, node):
    """解析判定结果；校验目标必须在 transitions 内，否则回退 defaultTo"""
    allowed = list(node.get("transitions") or [])
    nid = None
    try:
        m = re.search(r"\{.*\}", str(text or ""), re.S)
        if m:
            data = json.loads(m.group(0))
            nid = data.get("next_node_id")
    except Exception:
        nid = None
    if isinstance(nid, str) and nid.strip() in allowed:
        return nid.strip()
    d = (node.get("defaultTo") or "").strip()
    if d:
        return d
    return allowed[0] if allowed else None


# ============================================================
# AI 自动写剧本（让 AI 依据角色设定 + 用户画像自己编剧情并推进）
# ============================================================
def _clamp(v, dflt, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = dflt
    return max(lo, min(hi, n))


def _strip_fence(s):
    s = str(s or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    return m.group(1).strip() if m else s


def _json_obj(text):
    """从模型输出里尽量取出一个 JSON 对象（容错）"""
    s = _strip_fence(text)
    d = _lenient_json(s)
    if isinstance(d, dict):
        return d
    blk = _first_balanced_object(s)
    if blk:
        d = _lenient_json(blk)
        if isinstance(d, dict):
            return d
    return None


def build_script_prompt(char, profile_text="", acts=5, rounds=4, recent="", world=""):
    """让 AI 为这段对话编写分幕**大纲**（具体每一幕在上演时再展开）"""
    name = (char or {}).get("name") or "角色"
    prof = ((char or {}).get("profile") or "").strip()
    acts = _clamp(acts, 5, 1, 12)
    parts = [P.part("script", "head", name=name, acts=acts)]
    if prof:
        parts.append(P.part("script", "char", name=name, prof=prof))
    if world:
        parts.append(P.part("script", "world", world=world))
    if profile_text:
        parts.append(P.part("script", "profile", profile_text=profile_text))
    if recent:
        parts.append(P.part("script", "recent", recent=recent))
    return "\n".join(parts)


def parse_plan(text, acts=5):
    """把 AI 输出的大纲收敛成 {"title":..., "acts":[{"title","summary"}, ...]}；失败返回 None"""
    data = _json_obj(text)
    if not isinstance(data, dict):
        return None
    raw = data.get("acts") or data.get("nodes") or data.get("scenes") or []
    if not isinstance(raw, list):
        return None
    items = [x for x in raw if isinstance(x, dict)][:_clamp(acts, 5, 1, 12)]
    out = []
    for it in items:
        title = str(it.get("title") or it.get("name") or "").strip()
        summary = str(it.get("summary") or it.get("prompt") or it.get("desc") or "").strip()
        if title or summary:
            out.append({"title": title, "summary": summary})
    if not out:
        return None
    return {"title": str(data.get("title") or "").strip(), "acts": out}


def plan_outline_text(plan, limit=400):
    """大纲 → 注入提示词 / 面板展示用的文本"""
    if not isinstance(plan, dict):
        return ""
    parts = []
    if plan.get("title"):
        parts.append("剧名：%s" % plan["title"])
    for i, a in enumerate(plan.get("acts") or []):
        s = str(a.get("summary") or "").replace("\n", " ")
        if len(s) > limit:
            s = s[:limit] + "…"
        parts.append("- 第 %d 幕 %s：%s" % (i + 1, a.get("title") or "", s))
    return "\n".join(parts)


def start_story(plan, rounds=4):
    """由大纲构造起始剧本（开头只放第 1 幕，后续各幕在上演时逐幕展开）"""
    acts_list = (plan or {}).get("acts") or []
    if not acts_list:
        return None
    a = acts_list[0]
    prompt = str(a.get("summary") or "").strip()
    if a.get("title"):
        prompt = ("【%s】%s" % (a["title"], prompt)).strip()
    if not prompt:
        return None
    node = {"id": "a1", "type": "limited_turn",
            "maxRounds": _clamp(rounds, 4, 1, 12),
            "transitions": [], "defaultTo": "", "prompt": prompt}
    st = normalize_story({"start": "a1", "nodes": [node]})
    if (plan or {}).get("title"):
        st["title"] = plan["title"]
    return st


def build_next_act_prompt(char, outline="", done=0, total=5, recent="", must_end=False, world=""):
    """一幕演完后，让 AI 展开下一幕（或收束）"""
    name = (char or {}).get("name") or "角色"
    parts = [P.part("next_act", "head", name=name, done=done, total=total)]
    if must_end:
        parts.append(P.part("next_act", "branch_end"))
    else:
        parts.append(P.part("next_act", "branch_next", next_no=done + 1))
    parts.append(P.part("next_act", "rules"))
    prof = ((char or {}).get("profile") or "").strip()
    if prof:
        parts.append(P.part("next_act", "char", name=name, prof=prof))
    if world:
        parts.append(P.part("next_act", "world", world=world))
    if outline:
        parts.append(P.part("next_act", "outline", outline=outline))
    if recent:
        parts.append(P.part("next_act", "recent", recent=recent))
    return "\n".join(parts)


def build_recap_prompt(prev_recap="", recent=""):
    """每幕结束时刷新「前情提要」，供后续每轮常驻注入，避免长线跑偏"""
    parts = [P.part("recap", "head")]
    if prev_recap:
        parts.append(P.part("recap", "prev", prev_recap=prev_recap))
    if recent:
        parts.append(P.part("recap", "recent", recent=recent))
    return "\n".join(parts)


def parse_next_act(text, node_id, rounds=4):
    """解析「展开下一幕」的结果 → 单个节点；判定收束时给 ending 节点。失败返回 None"""
    data = _json_obj(text)
    if not isinstance(data, dict):
        return None
    it = data.get("act") if isinstance(data.get("act"), dict) else data
    prompt = str(it.get("prompt") or it.get("summary") or "").strip()
    if not prompt:
        return None
    title = str(it.get("title") or "").strip()
    if title:
        prompt = ("【%s】%s" % (title, prompt)).strip()
    if data.get("ending") or it.get("ending"):
        return {"id": node_id, "type": "ending", "maxRounds": 0,
                "transitions": [], "defaultTo": "", "prompt": prompt}
    return {"id": node_id, "type": "limited_turn",
            "maxRounds": _clamp(it.get("maxRounds") or it.get("max_rounds"), rounds, 1, 12),
            "transitions": [], "defaultTo": "", "prompt": prompt}


def append_node(story, node):
    """把新展开的一幕追加进剧本，并把上一幕的 transitions 指向它

    注意：normalize_story() 只保留 start/nodes，会丢掉 title —— 所以在归一化**之前**
    先把剧名取出来，否则每加一幕剧名就没了。"""
    title = (story or {}).get("title") if isinstance(story, dict) else None
    st = normalize_story(story)
    nodes = list(st.get("nodes") or [])
    if not node:
        return st
    if not nodes:
        out0 = normalize_story({"start": node["id"], "nodes": [node]})
        if title:
            out0["title"] = title
        return out0
    prev = dict(nodes[-1])
    prev["transitions"] = [node["id"]]
    prev["defaultTo"] = node["id"]
    nodes[-1] = prev
    nodes.append(node)
    out = normalize_story({"start": st.get("start") or nodes[0]["id"], "nodes": nodes})
    if title:
        out["title"] = title
    return out


def acts_done(story):
    """已展开并上演的幕数（节点个数）"""
    return len(normalize_story(story).get("nodes") or [])


# ============================================================
# 2. 情绪 → 立绘匹配
# ============================================================
def match_sprite(char, emotion=None, sprite=None):
    """返回立绘下标（0 起）；无立绘返回 None。

    优先级：显式编号(sprite, 1 起) → 情绪标签模糊匹配（数组按顺序）→ 默认第 1 张。
    """
    sprites = (char or {}).get("sprites") or []
    if not sprites:
        return None
    if isinstance(sprite, int) and 1 <= sprite <= len(sprites):
        return sprite - 1
    tags = emotion if isinstance(emotion, list) else ([emotion] if emotion else [])
    for e in tags:
        e = str(e or "").strip()
        if not e:
            continue
        for i, s in enumerate(sprites):
            for t in (s.get("emotion_tags") or []):
                if e == t or e in t or t in e:
                    return i
    return 0


# ============================================================
# 3. 输出契约：提示词
# ============================================================
_EXAMPLE_MULTI = ('{"dialog":['
                  '{"speaker":"","speech":"……你来啦。今天风很轻。","emotion":["微笑","放松"],"sprite":2},'
                  '{"speaker":"NARR","speech":"（她转过身，望向海面）"},'
                  '{"speaker":"","speech":"坐一会儿吗？","emotion":["放松"]}'
                  '],"options":[{"text":"「是啊，春天快到了。」"},{"text":"「你怎么在这儿？」"}]}')
_EXAMPLE_SINGLE = ('{"dialog":['
                   '{"speaker":"","speech":"……你来啦。今天风很轻。","emotion":"微笑","sprite":2},'
                   '{"speaker":"NARR","speech":"（她转过身，望向海面）"},'
                   '{"speaker":"","speech":"坐一会儿吗？","emotion":"放松"}'
                   '],"options":[{"text":"「是啊，春天快到了。」"},{"text":"「你怎么在这儿？」"}]}')


def build_prompt(char, emotion_multi=False, agent_on=False, effects=None, stage=None,
                 mem_on=False):
    """生成 GAL 演出模式的系统提示片段（追加在 system 提示末尾）"""
    name = (char or {}).get("name") or "角色"
    profile = ((char or {}).get("profile") or "").strip()
    sprites = (char or {}).get("sprites") or []
    roster = []
    for i, sp in enumerate(sprites):
        tags = "、".join((sp or {}).get("emotion_tags") or [])
        roster.append("%d%s" % (i + 1, ("：" + tags) if tags else ""))
    hint = ("字符串数组，按重要性从高到低，如 \"emotion\":[\"紧张\",\"小声\"]"
            if emotion_multi else "单个词，如 \"emotion\":\"微笑\"")
    example = _EXAMPLE_MULTI if emotion_multi else _EXAMPLE_SINGLE
    roster_hint = (("**换表情请直接给编号**，本角色可用立绘：" + "；".join(roster))
                   if roster else "")
    parts = [P.part("gal_contract", "head", name=name, hint=hint,
                    roster_hint=roster_hint, example=example)]
    roster_bg, _bg_names = background_roster()
    if roster_bg:
        parts.append(P.part("gal_contract", "bg_yes", roster_bg=roster_bg))
    else:
        parts.append(P.part("gal_contract", "bg_no"))
    roster_bgm, _bgm_names = bgm_roster()
    if roster_bgm:
        parts.append(P.part("gal_contract", "bgm_yes", roster_bgm=roster_bgm))
    else:
        parts.append(P.part("gal_contract", "bgm_no"))
    parts.append(P.part("gal_contract", "mid",
                        icons=" / ".join(STAT_ICON_NAMES), stat_max=STAT_MAX))
    # 当前舞台现状（状态参数 / 背景）每轮回灌，AI 才不会把数值与场景搞丢
    st = stage or {}
    st_items = [x for x in (normalize_stat(i) for i in (st.get("stats") or [])) if x]
    bg_name = _bg_display_name(st.get("bg"))
    bgm_name = _bgm_display_name(st.get("bgm"))
    if st_items or bg_name or bgm_name:
        parts.append(P.part("gal_contract", "stage_head"))
        if st_items:
            bits = []
            for it in st_items:
                label = it.get("label") or it["key"]
                if isinstance(it.get("value"), int):
                    mx = it.get("max")
                    bits.append("%s %d%s" % (label, it["value"],
                                             ("/%d" % mx) if isinstance(mx, int) else ""))
                else:
                    bits.append("%s「%s」" % (label, it.get("value")))
            parts.append(P.part("gal_contract", "stage_stats", stats_text="；".join(bits)))
        if bg_name:
            parts.append(P.part("gal_contract", "stage_bg", bg_name=bg_name))
        if bgm_name:
            parts.append(P.part("gal_contract", "stage_bgm", bgm_name=bgm_name))
        parts.append(P.part("gal_contract", "stage_foot"))
    fx = effect_catalog_text(effects)
    if fx:
        parts.append(fx)
    if agent_on:
        parts.append(P.part("gal_contract", "agent"))
    if mem_on:
        parts.append(P.part("gal_contract", "mem"))
    if profile:
        parts.append(P.part("gal_contract", "profile", name=name, profile=profile))
    return "\n".join(parts)


# ============================================================
# 4. 流式解析：按完整 JSON 对象逐段吐出对白
# ============================================================
NARR_ALIASES = ("narr", "narration", "nar", "旁白", "旁白：", "旁白:")


def _is_narr(name):
    s = str(name or "").strip()
    return s.lower() in NARR_ALIASES or s in NARR_ALIASES


def normalize_dialog_item(it, char_name=""):
    """把一段对白收敛成 {speaker, speech, narration, emotion, sprite}

    speaker 为空或等于当前角色名 = 当前角色；NARR/旁白 = 旁白（不朗读）。
    """
    if not isinstance(it, dict):
        return None
    # 只认 speech / narration：避免把 options 里的 {"text": ...} 误当成对白段
    speech = str(it.get("speech") or "").strip()
    narr = str(it.get("narration") or "").strip()
    if not speech and not narr:
        return None
    speaker = str(it.get("speaker") or it.get("character_name") or it.get("name") or "").strip()
    if speaker and char_name and speaker == char_name:
        speaker = ""
    em = it.get("emotion")
    if isinstance(em, list):
        em = [str(x).strip() for x in em if str(x).strip()]
    elif em is None:
        em = ""
    else:
        em = str(em).strip()
    try:
        sprite = int(it.get("sprite")) if it.get("sprite") is not None else None
    except (TypeError, ValueError):
        sprite = None
    if _is_narr(speaker) or (not speech and narr):
        return {"speaker": "NARR", "speech": "", "narration": (narr or speech),
                "emotion": em, "sprite": sprite}
    return {"speaker": speaker, "speech": speech, "narration": "", "emotion": em, "sprite": sprite}


def _dialog_from_obj(obj, char_name=""):
    """把已解析的 JSON 对象转成 [段, ...]（兼容新契约 dialog 数组与旧扁平对象）"""
    items = []
    if isinstance(obj.get("dialog"), list):
        items = [x for x in obj["dialog"] if isinstance(x, dict)]
    else:
        narr = obj.get("narration")
        if isinstance(narr, str) and narr.strip():
            items.append({"speaker": "NARR", "speech": narr})
        items.append(obj)
    out = []
    for it in items:
        seg = normalize_dialog_item(it, char_name)
        if seg and (seg["speech"] or seg["narration"]):
            out.append(seg)
    return out


def _complete_json_object_span(text):
    """返回第一个能闭合的顶层 {...} 的 (start, end)；忽略字符串内的括号、支持嵌套。

    流式时内层条目先于外层包裹闭合，因此遍历所有 `{` 起点即可优先拿到内层对象。
    """
    starts = [i for i, c in enumerate(text) if c == "{"][:200]
    for start in starts:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return start, i + 1
                if depth < 0:
                    break
    return None


class DialogStreamParser:
    """从流式文本里按「完整的 JSON 对象」逐段吐出对白（与 Shinsekai 的 stream_parser 同思路）。

    契约：{"dialog":[{...},{...}], "options":[...], ...}
    """

    def __init__(self, char_name="", effects=None):
        self._buf = ""
        self.char_name = char_name or ""
        self.effects = effects or []
        self.count = 0

    def feed(self, chunk):
        out = []
        if chunk:
            self._buf += chunk
        while "}" in self._buf:
            span = _complete_json_object_span(self._buf)
            if not span:
                break
            start, end = span
            raw = self._buf[start:end]
            self._buf = self._buf[end:]
            obj = _lenient_json(raw)
            if not isinstance(obj, dict):
                continue
            for seg in _dialog_from_obj(obj, self.char_name):
                if self.effects:                    # 抽出 *关键词* 特效并从文本里移除
                    if seg["speech"]:
                        seg["speech"], seg["fx"] = extract_effects(seg["speech"], self.effects)
                    elif seg["narration"]:
                        seg["narration"], seg["fx"] = extract_effects(seg["narration"], self.effects)
                self.count += 1
                seg["index"] = self.count
                out.append(seg)
        return out


# ============================================================
# 5. 完整回复解析（容错）
# ============================================================
def _first_balanced_object(s):
    """截取第一个括号平衡的 {...}（忽略字符串内的括号）"""
    start = s.find("{")
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return ""


def _fix_latex_backslashes(s):
    """把模型漏转义的 LaTeX 命令补成合法 JSON 转义：\\frac→\\\\frac、\\times→\\\\times。

    只处理 b/f/r/t（JSON 里合法但这里几乎不可能是本意的转义）；\\n 保留为换行。
    已写成 \\\\ 的（前面还有反斜杠）不动。
    """
    return re.sub(r'(?<!\\)\\([bfrt])', r'\\\\\1', s)


def _lenient_json(s):
    """json.loads 的容错版本：优先修复 LaTeX 漏转义，再对非法转义补反斜杠"""
    if not isinstance(s, str):
        return None
    opts = [_fix_latex_backslashes(s), s]
    opts += [re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', o) for o in opts]
    for cand in opts:
        try:
            d = json.loads(cand)
        except Exception:
            continue
        if isinstance(d, dict):
            return d
    return None


def _norm_options(raw):
    opts = []
    for it in (raw or []):
        if isinstance(it, dict):
            t = str(it.get("text") or "").strip()
        elif isinstance(it, str):
            t = it.strip()
        else:
            t = ""
        if not t:
            continue
        o = {"text": t}
        if isinstance(it, dict):
            tag = str(it.get("tag") or "").strip()[:8]
            if tag:
                o["tag"] = tag
            hint = str(it.get("hint") or it.get("note") or "").strip()[:24]
            if hint:
                o["hint"] = hint
            if it.get("ok") is False:            # 条件不满足：前端置灰不可点
                o["ok"] = False
        opts.append(o)
    return opts[:6]


# ============================================================
# 5b. 按需判定（骰子）：只在「成败不确定」或「需要随机数值」时才用
# ============================================================
ROLL_DICE_MAX = 20        # 一次最多掷几个骰子
ROLL_FACES_MAX = 1000     # 骰面上限


def parse_roll_spec(text):
    """从模型回复里取出「只申请判定、还没演出」的 roll 请求；返回规格 dict 或 None

    仅当回复是纯 JSON 且**没有 dialog** 时才认——正文里夹带 roll 的一律忽略，
    避免同一轮既演出又掷骰造成重复。
    """
    s = str(text or "").strip()
    if not s:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    data = _json_obj(s)
    if not isinstance(data, dict):
        return None
    if data.get("dialog"):
        return None
    spec = data.get("roll")
    return spec if isinstance(spec, dict) else None


def _parse_dice(expr):
    """解析 NdM+K 表达式 → (个数, 面数, 加值)；不合法返回 None"""
    s = re.sub(r"\s+", "", str(expr or "")).lower()
    m = re.fullmatch(r"(\d*)d(\d+)([+-]\d+)?", s)
    if not m:
        return None
    cnt = int(m.group(1) or 1)
    faces = int(m.group(2))
    bonus = int(m.group(3) or 0)
    if not (1 <= cnt <= ROLL_DICE_MAX and 2 <= faces <= ROLL_FACES_MAX):
        return None
    return cnt, faces, bonus


def do_roll(spec):
    """执行一次判定 / 掷骰；返回 {why, detail, success, value, prompt}；不合法返回 None

    两种写法：
    - 检定式 {"dc":14,"mod":2[, "why":"撬锁"]}  → 掷 d20+mod 与 DC 比较
    - 自由骰 {"dice":"2d6+1"[, "why":"伤害"]}   → 掷表达式，直接给数值
    """
    if not isinstance(spec, dict):
        return None
    why = str(spec.get("why") or spec.get("reason") or "").strip()[:40]
    parsed = _parse_dice(spec.get("dice")) if spec.get("dice") else None
    if parsed:
        cnt, faces, bonus = parsed
        rolls = [random.randint(1, faces) for _ in range(cnt)]
        s = sum(rolls)
        total = s + bonus
        body = " + ".join(str(r) for r in rolls)
        detail = ("掷 %dd%d 得 %s = %d，加值 %+d，合计 %d" % (cnt, faces, body, s, bonus, total)
                  if bonus else "掷 %dd%d 得 %s = %d" % (cnt, faces, body, total))
        return {
            "why": why, "detail": detail, "success": None, "value": total,
            "prompt": "【系统掷骰】%s：%s。请把 **%d** 这个数值用进演出（伤害 / 掉落 / 数量等），"
                      "**不要改动这个数字**，也不要再申请一次判定。" % (why or "随机数值", detail, total),
        }
    dc = _stat_num(spec.get("dc"))
    if dc is None:
        return None
    mod = _stat_num(spec.get("mod")) or 0
    d = random.randint(1, 20)
    total = d + mod
    ok = total >= dc
    res = "成功" if ok else "失败"
    if mod:
        detail = "掷 d20 得 %d，加值 %+d，合计 %d；需 ≥ %d → %s" % (d, mod, total, dc, res)
    else:
        detail = "掷 d20 得 %d；需 ≥ %d → %s" % (d, dc, res)
    return {
        "why": why, "detail": detail, "success": bool(ok), "value": total,
        "prompt": "【系统判定】%s：%s。请**严格按这个结果**继续演出——成功就写成成功及其收益，"
                  "失败就写成失败及其后果，**不要推翻判定**，也不要再申请一次判定。"
                  % (why or "检定", detail),
    }


def detect_stream_mode(sample):
    """流式开头判断模型走 JSON 契约还是普通文本：返回 "json" / "text" / None（还不足以判断）。

    会话里若混有普通文本回复，模型常会模仿它而丢掉 GAL 的 JSON 契约；
    这里让后端能及早改走「纯文本兜底分段」，保证逐段演出不失效。
    """
    s = (sample or "").lstrip()
    if not s:
        return None
    if s[0] == "{":
        return "json"
    if s[0] == "`":                       # 可能是 ```json 围栏：跳过围栏行再看
        nl = s.find("\n")
        if nl < 0:
            return None
        rest = s[nl + 1:].lstrip()
        if not rest:
            return None
        return "json" if rest[0] == "{" else "text"
    return "text"


def split_plain_chunks(buf, flush=False):
    """普通文本兜底：按换行切成可展示的段，返回 (可立即展示的段列表, 仍需缓冲的尾巴)。"""
    if flush:
        return [p for p in re.split(r"\n+", buf) if p.strip()], ""
    parts = re.split(r"\n+", buf)
    return [p for p in parts[:-1] if p.strip()], parts[-1]


def _norm_cast(raw):
    """舞台立绘 cast：AI 给的立绘编号数组 → 去重、只留正整数、最多 3 个"""
    if isinstance(raw, (int, float)):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n >= 1 and n not in out:
            out.append(n)
        if len(out) >= 3:
            break
    return out


def _norm_map(raw):
    """平面俯视图：{"place": 地点, "marks":[{"name","x","y"}], "links":[{"a","b","label"}]}

    x / y 为 0–1 归一化坐标（0,0 = 西北角）。兼容旧写法：标记只给 `pos` 方位时
    按九宫格换算成坐标，历史会话不用清理。"""
    if not isinstance(raw, dict):
        return None
    place = str(raw.get("place") or "").strip()[:40]
    marks = []
    for m in (raw.get("marks") or [])[:12]:
        if not isinstance(m, dict):
            continue
        nm = str(m.get("name") or "").strip()[:16]
        if not nm:
            continue
        x = _num01(m.get("x"))
        y = _num01(m.get("y"))
        if x is None or y is None:                       # 旧格式：用方位换算
            x, y = MAP_POS_XY.get(str(m.get("pos") or "").strip(), MAP_POS_XY["中"])
        marks.append({"name": nm, "x": round(x, 3), "y": round(y, 3)})
    names = set(m["name"] for m in marks)
    links = []
    for lk in (raw.get("links") or [])[:16]:
        if not isinstance(lk, dict):
            continue
        a = str(lk.get("a") or "").strip()[:16]
        b = str(lk.get("b") or "").strip()[:16]
        if a and b and a != b and a in names and b in names:
            links.append({"a": a, "b": b, "label": str(lk.get("label") or "").strip()[:8]})
    if not place and not marks:
        return None
    return {"place": place, "marks": marks, "links": links}


def _num01(v):
    """把值夹到 0–1；非法 / 缺失返回 None"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:                                          # NaN
        return None
    return max(0.0, min(1.0, f))



def parse_reply(text, char_name="", effects=None):
    """把模型回复解析成 {dialog, options, bg, bgm}；失败返回 None（兼容旧扁平对象）"""
    if not text or not str(text).strip():
        return None
    s = str(text).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)     # 剥掉代码围栏
    if m:
        s = m.group(1).strip()
    data = _lenient_json(s)
    if not isinstance(data, dict):
        blk = _first_balanced_object(s)
        if blk:
            data = _lenient_json(blk)
    if not isinstance(data, dict):
        return None
    dialog = _dialog_from_obj(data, char_name)
    if not dialog:
        return None
    if effects:                                            # 与流式路径保持一致：抽特效
        for seg in dialog:
            if seg["speech"]:
                seg["speech"], seg["fx"] = extract_effects(seg["speech"], effects)
            elif seg["narration"]:
                seg["narration"], seg["fx"] = extract_effects(seg["narration"], effects)
    return {
        "dialog": dialog,
        "options": _norm_options(data.get("options")),
        # 本轮没有给 options 时，模型可给一句「邀请玩家开口」的话（贴合当前场景口吻），
        # 前端在最后一段对白末尾显示；给了 options 时前端不会用它
        "nudge": str(data.get("nudge") or "").strip()[:80],
        "bg": str(data.get("bg") or "").strip(),
        "bgm": str(data.get("bgm") or "").strip(),
        "cast": _norm_cast(data.get("cast")) if "cast" in data else None,
        "map": _norm_map(data.get("map")),
        "stats": _norm_stats(data.get("stats")),
    }


def joined_speech(dialog):
    """把非旁白段的台词拼起来（用于会话历史 / 兜底朗读）"""
    return "\n".join(s["speech"] for s in (dialog or []) if s.get("speech"))
