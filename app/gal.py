# -*- coding: utf-8 -*-
"""GAL 演出模式：角色/立绘资源、输出契约提示词、流式 speech 抽取、回复解析、情绪匹配

设计见 docs/GAL_MODE_DESIGN.md（本地文档）。
本模块只做「数据 + 契约 + 解析」，不涉及 HTTP；路由与聊天流集成在 app/httpd.py。
"""
import json
import os
import re
import shutil

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
    lines = ["## 剧情节点（%s，类型 %s）" % (node["id"], node["type"])]
    if node.get("prompt"):
        lines.append(node["prompt"])
    if node["type"] == "limited_turn":
        lines.append("本节点已进行 %d / %d 轮；接近上限时自然收束，为切换场景做准备。"
                     % (round_no, node["maxRounds"]))
    elif node["type"] == "ending":
        lines.append("这是终章：给出收束性的结局，不再引入新支线。")
    return "\n".join(lines)


def build_choice_prompt(node):
    """让模型选择下一个节点（只返回一个 JSON）"""
    opts = list(node.get("transitions") or [])
    return ("当前剧情节点「%s」已达回合上限。请根据刚才的对话走向，从下列候选里选择下一个节点：%s。"
            "只输出一个 JSON：{\"next_node_id\":\"<候选之一>\"}；若都不合适，输出 {\"next_node_id\":null}。"
            % (node["id"], "、".join(opts) if opts else "（无）"))


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


def build_script_prompt(char, profile_text="", acts=5, rounds=4, recent=""):
    """让 AI 为这段对话编写分幕**大纲**（具体每一幕在上演时再展开）"""
    name = (char or {}).get("name") or "角色"
    prof = ((char or {}).get("profile") or "").strip()
    acts = _clamp(acts, 5, 1, 12)
    lines = [
        "## 任务：为一段 Galgame 对话编写分幕大纲",
        "你要扮演**编剧**，为角色「%s」与玩家之间的一整段故事规划 %d 幕。" % (name, acts),
        "要求：",
        "- **必须以「角色设定」为唯一准绳**：性格、口癖、说话方式、人际关系、背景都不得违背。",
        "- 若提供了「玩家画像」，剧情要贴合这些偏好，且**不得编造与画像冲突的喜好或经历**。",
        "- 每一幕要有明确的情境、目标与情绪走向，并自然衔接下一幕；不要写成流水账。",
        "- 整体节奏：序幕（相遇 / 日常）→ 若干发展（升温 / 波折 / 坦白）→ 尾声（收束）。",
        "- 最后一幕指向结局方向，不引入新支线。",
        "- `summary` 是这一幕的**概要**（一句话到 60 字），不是台词，用来指导后续上演时的展开。",
        "**只输出一个 JSON 对象**（不要代码围栏、不要在 JSON 之外写任何文字）：",
        '{"title":"剧名","acts":[{"title":"幕标题","summary":"这一幕的剧情概要"}]}',
        "`acts` 数组必须正好 %d 项。" % acts,
    ]
    if prof:
        lines += ["", "### 角色设定（%s）" % name, prof]
    if profile_text:
        lines += ["", "### 玩家画像（用于贴合，不要直接复述）", profile_text]
    if recent:
        lines += ["", "### 已有对话（作为起点参考）", recent]
    return "\n".join(lines)


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


def plan_outline_text(plan, limit=110):
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


def build_next_act_prompt(char, outline="", done=0, total=5, recent="", must_end=False):
    """一幕演完后，让 AI 展开下一幕（或收束）"""
    name = (char or {}).get("name") or "角色"
    lines = [
        "## 任务：展开下一幕",
        "角色「%s」的故事已演完 %d / %d 幕。" % (name, done, total),
    ]
    if must_end:
        lines.append("**这一幕是收尾**：请写出最终一幕（结局），给整段故事一个完整的收束，不再引入新支线。")
        lines.append('只输出：{"ending":true,"title":"结局幕标题","prompt":"该幕的演出要求（60–150 字）"}')
    else:
        lines.append("请依据大纲与刚才的对话走向，展开**第 %d 幕**：承接已有的情节与情绪，并给出新的推进。"
                     % (done + 1))
        lines.append('只输出：{"act":{"title":"幕标题","prompt":"该幕的演出要求（60–150 字）","maxRounds":4}}')
        lines.append('若剧情已到自然终点，可改为输出 {"ending":true,"prompt":"..."} 来收束。')
    lines += [
        "- 必须符合角色设定，不得让人物说出不符合性格的话。",
        "- `prompt` 写给扮演该角色的 AI 看：第三人称描述情境与推进方向，不要直接写成段台词。",
    ]
    prof = ((char or {}).get("profile") or "").strip()
    if prof:
        lines += ["", "### 角色设定（%s）" % name, prof]
    if outline:
        lines += ["", "### 全剧大纲（供参考，可适度调整）", outline]
    if recent:
        lines += ["", "### 刚才的对话", recent]
    return "\n".join(lines)


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
    """把新展开的一幕追加进剧本，并把上一幕的 transitions 指向它"""
    st = normalize_story(story)
    nodes = list(st.get("nodes") or [])
    if not node:
        return st
    if not nodes:
        return normalize_story({"start": node["id"], "nodes": [node]})
    prev = dict(nodes[-1])
    prev["transitions"] = [node["id"]]
    prev["defaultTo"] = node["id"]
    nodes[-1] = prev
    nodes.append(node)
    out = normalize_story({"start": st.get("start") or nodes[0]["id"], "nodes": nodes})
    if st.get("title"):
        out["title"] = st["title"]
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
                  '{"speaker":"","speech":"坐一会儿吗？"}'
                  '],"options":[{"text":"「是啊，春天快到了。」"},{"text":"「你怎么在这儿？」"}]}')
_EXAMPLE_SINGLE = ('{"dialog":['
                   '{"speaker":"","speech":"……你来啦。今天风很轻。","emotion":"微笑","sprite":2},'
                   '{"speaker":"NARR","speech":"（她转过身，望向海面）"},'
                   '{"speaker":"","speech":"坐一会儿吗？"}'
                   '],"options":[{"text":"「是啊，春天快到了。」"},{"text":"「你怎么在这儿？」"}]}')


def build_prompt(char, emotion_multi=False, agent_on=False, effects=None):
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
    lines = [
        "## GAL 演出模式",
        "你现在在扮演角色「%s」与用户进行 Galgame 式对话。" % name,
        "**只输出一个 JSON 对象**（不要代码围栏、不要在 JSON 之外写任何解释文字）。",
        "顶层字段：",
        "- `dialog`（必填）：**对白数组**。把这一轮要说的话拆成若干短段（建议 1–6 段，每段 1–2 句）；"
        "观众会一段一段地看和听，所以不要一口气写成一大段。",
        "- **每一轮请至少用一段 `NARR` 旁白**交代场景 / 氛围 / 动作（放在 `dialog` 开头，简短一两句），"
        "让读者知道现在的情境（否则会不知道故事发生在哪、在做什么）。",
        "- `options`（可选）：数组，每项 {\"text\":\"选项内容\"}，给用户 2–4 个可选回应",
        "- `bg` / `bgm`（可选）：切换背景 / 背景音乐（填资源相对路径）",
        "`dialog` 每一段的字段：",
        "- `speaker`（可选）：说话者；留空或填「" + name + "」表示当前角色，填 `NARR` 表示旁白（不朗读）",
        "- `speech`（必填）：该段要说的话",
        "- `emotion`（可选）：情绪标签，" + hint,
        "- `sprite`（可选）：立绘编号（整数，从 1 开始）；填写时优先于 emotion。"
        + (("**换表情请直接给编号**，本角色可用立绘：" + "；".join(roster)) if roster else ""),
        "排版约定（写在 `speech` 的**字符串内部**，不要写在 JSON 之外）：",
        "- 可用 Markdown：`**强调**`、`` `代码` ``、有序/无序列表、表格、`>` 引用；换行用 `\\n`。",
        "- 数学公式用 LaTeX：行内 `$...$`，块级 `$$...$$`（会被排版渲染，不会按原始符号朗读）。",
        "- **JSON 字符串里的反斜杠必须写成两个**：例如 `\\\\frac{1}{2}`、`\\\\times`、`\\\\alpha`、`\\\\n`。",
        "示例：",
        example,
    ]
    fx = effect_catalog_text(effects)
    if fx:
        lines.append(fx)
    if agent_on:
        lines.append("需要调用工具时，先照常输出 ```tool 块；**最终答复**仍必须是上面的 JSON。")
    if profile:
        lines += ["", "### 角色设定（%s）" % name, profile]
    return "\n".join(lines)


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
        if t:
            opts.append({"text": t})
    return opts[:6]


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
        "bg": str(data.get("bg") or "").strip(),
        "bgm": str(data.get("bgm") or "").strip(),
    }


def joined_speech(dialog):
    """把非旁白段的台词拼起来（用于会话历史 / 兜底朗读）"""
    return "\n".join(s["speech"] for s in (dialog or []) if s.get("speech"))
