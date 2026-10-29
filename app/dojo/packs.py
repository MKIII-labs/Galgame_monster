# -*- coding: utf-8 -*-
"""场景包加载与校验。

一个包 = 一个目录：
    resources/dojo/packs/<pack_id>/manifest.json
    resources/dojo/packs/<pack_id>/units/*.json

引擎不认识「数学」「建模」「科研」，只认识这 8 个能力维度与 8 个原语；
场景的全部差异都落在包里。校验失败包会被跳过并打印原因，而不是带着坏数据上线。
"""
import hashlib
import json
import os
import re
import shutil

from app.store import DATA_DIR, RESOURCES_DIR, read_json
from app.dojo.primitives import JUDGE_LEVELS, PRIMITIVES

PACKS_DIR = os.path.join(RESOURCES_DIR, "dojo", "packs")
# 用户导入的包写在这里（data 目录整目录 gitignore），不污染随项目发布的内置包
USER_PACKS_DIR = os.path.join(DATA_DIR, "dojo", "packs")
_UNIT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# 研究能力的 8 个原子动作：跨学科共用，画像与复习都按这 8 维聚合
ABILITIES = (
    "framing", "assumption", "method", "condition",
    "evidence", "robustness", "argument", "reflection",
)
ABILITY_NAMES = {
    "framing": "定义问题",
    "assumption": "建立假设",
    "method": "选择方法",
    "condition": "判断适用性",
    "evidence": "证据",
    "robustness": "稳健性",
    "argument": "论证",
    "reflection": "反思迁移",
}
GRADES = ("official", "verified", "personal")

_CACHE = {"sig": None, "packs": []}


class PackError(Exception):
    pass


def _unit_files(pack_dir):
    d = os.path.join(pack_dir, "units")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".json")]


def _check_unit(unit, grade):
    """返回 (errors, warnings)。errors 会让整个包不可用。"""
    errs, warns = [], []
    uid = unit.get("id") or "（缺 id）"
    if unit.get("id") and not _UNIT_ID.match(str(unit["id"])):
        errs.append("%s 的 id 只能用字母 / 数字 / _ . -（≤64 字符）" % uid)

    def need(field, value):
        if value is None or value == "" or value == [] or value == {}:
            errs.append("%s 缺少 %s" % (uid, field))

    for field in ("id", "ability", "primitive", "judge", "minutes", "payload", "expert", "card"):
        need(field, unit.get(field))

    ability = unit.get("ability")
    if ability and ability not in ABILITIES:
        errs.append("%s 的能力维度不认识：%s" % (uid, ability))
    primitive = unit.get("primitive")
    if primitive and primitive not in PRIMITIVES:
        errs.append("%s 的原语不认识：%s" % (uid, primitive))
    if unit.get("judge") and unit["judge"] not in JUDGE_LEVELS:
        errs.append("%s 的判定等级不认识：%s" % (uid, unit["judge"]))
    minutes = unit.get("minutes")
    if isinstance(minutes, (int, float)) and not (0 < minutes <= 60):
        errs.append("%s 的 minutes 应在 1–60 之间" % uid)

    expert = unit.get("expert") or {}
    if expert and not str(expert.get("why") or "").strip():
        errs.append("%s 的 expert.why 不能为空（没有理由就不是训练）" % uid)
    if expert and not (expert.get("signals") or []):
        errs.append("%s 的 expert.signals 至少给一条" % uid)

    card = unit.get("card") or {}
    for field in ("title", "trigger", "first_step", "pitfall"):
        if not str(card.get(field) or "").strip():
            errs.append("%s 的方法卡缺少 %s" % (uid, field))

    source = unit.get("source_ref") or {}
    if not str(source.get("doc") or "").strip():
        errs.append("%s 缺少 source_ref.doc（无出处的内容不进训练队列）" % uid)

    payload = unit.get("payload") or {}
    if primitive == "single_choice":
        options = payload.get("options") or []
        keys = [str(o.get("key") or chr(65 + i)) if isinstance(o, dict) else chr(65 + i)
                for i, o in enumerate(options)]
        if len(keys) < 2:
            errs.append("%s 至少要有两个选项" % uid)
        want = str(expert.get("choice") or "").upper()
        if want and want not in [k.upper() for k in keys]:
            errs.append("%s 的标准选项不在选项里" % uid)
        traps = expert.get("traps") or {}
        missing = [k for k in keys if k.upper() != want and k not in traps and k.upper() not in traps]
        if missing:
            msg = "%s 的 traps 没覆盖错误选项：%s" % (uid, ",".join(missing))
            (errs if grade in ("official", "verified") else warns).append(msg)

    elif primitive == "multi_select":
        options = payload.get("options") or []
        keys = {str(o.get("key") or chr(65 + i)).upper() if isinstance(o, dict) else chr(65 + i)
                for i, o in enumerate(options)}
        if len(keys) < 3:
            errs.append("%s 至少要有三个选项" % uid)
        want = {str(x).upper() for x in (expert.get("choice") or [])}
        if not want:
            errs.append("%s 缺少 expert.choice" % uid)
        elif not want <= keys:
            errs.append("%s 的标准选项不在选项里" % uid)

    elif primitive == "order":
        steps = {str(s.get("id")) for s in (payload.get("steps") or [])}
        want = [str(x) for x in (expert.get("order") or [])]
        if len(steps) < 2:
            errs.append("%s 至少要有两个步骤" % uid)
        if steps and set(want) != steps:
            errs.append("%s 的 order 必须不重不漏地覆盖 steps" % uid)

    elif primitive == "pair":
        left = {str(x.get("id")) for x in (payload.get("left") or [])}
        right = {str(x.get("id")) for x in (payload.get("right") or [])}
        pairs = expert.get("pairs") or {}
        if len(left) < 2 or len(right) < 2:
            errs.append("%s 的配对至少各有两项" % uid)
        if pairs and (set(pairs) != left or not set(pairs.values()) <= right):
            errs.append("%s 的 pairs 与左右项对不上" % uid)

    elif primitive == "numeric":
        if not isinstance(expert.get("value"), (int, float)):
            errs.append("%s 的 expert.value 必须是数字" % uid)

    elif primitive == "expr":
        if not str(expert.get("expr") or "").strip():
            errs.append("%s 缺少 expert.expr" % uid)

    elif primitive == "annotate":
        segments = {str(s.get("id")) for s in (payload.get("segments") or [])}
        want = {str(x) for x in (expert.get("segments") or [])}
        if len(segments) < 2:
            errs.append("%s 至少要有两个句子" % uid)
        if not want or (segments and not want <= segments):
            errs.append("%s 的 key segments 不在句子里" % uid)

    elif primitive == "short_text":
        rubric = expert.get("rubric") or []
        if not rubric:
            errs.append("%s 缺少 expert.rubric（否则无从诊断）" % uid)
        for item in rubric:
            if not item.get("id") or not (item.get("keywords") or []):
                errs.append("%s 的 rubric 每项都要有 id 与 keywords" % uid)

    return errs, warns


def _load_pack(pack_dir, user=False):
    manifest = read_json(os.path.join(pack_dir, "manifest.json"), None)
    if not isinstance(manifest, dict):
        raise PackError("manifest.json 读取失败")
    if user:
        # 用户导入的包只能算 personal：不允许自封 official / verified 进入提分报告
        manifest = dict(manifest)
        manifest["grade"] = "personal"
    errs, warns = [], []

    for field in ("id", "name", "version", "alignment", "ability_weights", "entry"):
        if not manifest.get(field):
            errs.append("manifest 缺少 %s" % field)
    grade = manifest.get("grade") or "personal"
    if grade not in GRADES:
        errs.append("manifest.grade 只能是 %s" % "/".join(GRADES))
    align = manifest.get("alignment") or {}
    if not align.get("standard") or not (align.get("dimensions") or []):
        errs.append("alignment 需要 standard 与 dimensions")
    weights = manifest.get("ability_weights") or {}
    if not isinstance(weights, dict) or not weights:
        errs.append("ability_weights 不能为空")
    else:
        bad = [k for k in weights if k not in ABILITIES]
        if bad:
            errs.append("ability_weights 里有不认识的能力：%s" % ",".join(bad))

    units, seen = [], set()
    for path in _unit_files(pack_dir):
        unit = read_json(path, None)
        if not isinstance(unit, dict):
            errs.append("%s 读取失败" % os.path.basename(path))
            continue
        if unit.get("id") in seen:
            errs.append("单元 id 重复：%s" % unit.get("id"))
        seen.add(unit.get("id"))
        e, w = _check_unit(unit, grade)
        errs.extend(e)
        warns.extend(w)
        units.append(unit)

    if not units:
        errs.append("包里没有单元")
    # 教学顺序由 manifest.sequence 决定；文件名只用于稳定加载，不承担排序语义。
    # 测评题（unit.test = pre/post）不进训练序列。
    train_ids = {u.get("id") for u in units if not u.get("test")}
    test_ids = {u.get("id") for u in units if u.get("test")}
    seq = manifest.get("sequence") or []
    if seq:
        if set(seq) != train_ids or len(seq) != len(train_ids):
            errs.append("manifest.sequence 必须不重不漏地覆盖所有训练单元（不含测评题）")
        else:
            rank = {uid: i for i, uid in enumerate(seq)}
            order = {u.get("id"): rank.get(u.get("id"), len(rank)) for u in units}
            units.sort(key=lambda u: order.get(u.get("id"), len(rank)))

    assess = manifest.get("assessment")
    if assess:
        if not isinstance(assess, dict) or not assess.get("pre") or not assess.get("post"):
            errs.append("manifest.assessment 需要 pre 与 post 两个 id 列表")
        else:
            for phase in ("pre", "post"):
                ids = [str(x) for x in (assess.get(phase) or [])]
                if not ids:
                    errs.append("assessment.%s 不能为空" % phase)
                bad = [x for x in ids if x not in test_ids]
                if bad:
                    errs.append("assessment.%s 引用了非测评题：%s" % (phase, ",".join(bad)))
            for u in units:
                tag = u.get("test")
                if tag and tag not in ("pre", "post"):
                    errs.append("%s 的 test 只能是 pre / post" % u.get("id"))

    primitives_used = {u.get("primitive") for u in units if u.get("primitive")}
    if grade in ("official", "verified") and len(primitives_used) < 3:
        errs.append("正式包至少要用 3 种原语（当前只有 %d 种）" % len(primitives_used))

    for msg in warns:
        print("[道场] 包 %s 警告：%s" % (manifest.get("id"), msg))
    if errs:
        for msg in errs:
            print("[道场] 包 %s 校验失败：%s" % (manifest.get("id"), msg))
        raise PackError("；".join(errs[:3]))
    return {"manifest": manifest, "units": units, "dir": pack_dir}


def _roots():
    """内置包在前、用户包在后：同 id 时内置优先，避免用户包覆盖官方内容。"""
    return ((PACKS_DIR, False), (USER_PACKS_DIR, True))


def _signature():
    sig = []
    for root, _user in _roots():
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            for path in [os.path.join(d, "manifest.json")] + _unit_files(d):
                try:
                    sig.append((path, os.path.getmtime(path)))
                except OSError:
                    pass
    return tuple(sig)


def load_packs():
    """扫描并缓存所有包。文件改动会自动触发重新加载（按 mtime）。"""
    sig = _signature()
    if _CACHE["sig"] == sig:
        return _CACHE["packs"]
    loaded, seen = [], set()
    for root, is_user in _roots():
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d) or name.startswith("."):
                continue
            try:
                pack = _load_pack(d, is_user)
            except PackError as e:
                print("[道场] 跳过包 %s：%s" % (name, e))
                continue
            pid = pack["manifest"]["id"]
            if pid in seen:
                print("[道场] 跳过重名包 %s（%s）" % (pid, "用户包" if is_user else "内置包"))
                continue
            seen.add(pid)
            loaded.append(pack)
    _CACHE["sig"] = sig
    _CACHE["packs"] = loaded
    return loaded


def reset_cache():
    _CACHE["sig"] = None
    _CACHE["packs"] = []


def get_pack(pack_id):
    for pack in load_packs():
        if pack["manifest"]["id"] == pack_id:
            return pack
    raise PackError("没有这个场景包：%s" % pack_id)


def public_manifest(pack):
    m = pack["manifest"]
    units = pack["units"]
    train = [u for u in units if not u.get("test")]
    assess = m.get("assessment") or {}
    return {
        "id": m["id"],
        "name": m.get("name") or m["id"],
        "desc": m.get("desc") or "",
        "version": m.get("version"),
        "grade": m.get("grade") or "personal",
        "alignment": m.get("alignment") or {},
        "ability_weights": m.get("ability_weights") or {},
        "entry": m.get("entry") or {},
        "unit_count": len(train),
        "minutes_total": sum(int(u.get("minutes") or 0) for u in train),
        # 只给「顺序 + 能力 + 时长」，不带题面，前端据此做下一题与进度
        "unit_index": [
            {"id": u["id"], "ability": u.get("ability"), "minutes": u.get("minutes")}
            for u in train
        ],
        "assessment": ({"pre": len(assess.get("pre") or []),
                        "post": len(assess.get("post") or [])} if assess else None),
    }


def public_unit(unit):
    """题面：绝不包含 expert / card（答案与理由只在提交后返回）。"""
    ability = unit.get("ability")
    return {
        "id": unit["id"],
        "ability": ability,
        "ability_name": ABILITY_NAMES.get(ability, ability),
        "primitive": unit.get("primitive"),
        "judge": unit.get("judge"),
        "minutes": unit.get("minutes"),
        "payload": unit.get("payload") or {},
    }


def public_packs():
    return [public_manifest(p) for p in load_packs()]


def pack_slug(name):
    """把场景名转成稳定的英文目录 id；中文名靠短哈希区分。"""
    base = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:24]
    digest = hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()[:6]
    return (base + "-" if base else "pack-") + digest


def write_pack(pack_id, manifest, units):
    """写入用户包：先建临时目录并跑完整校验，通过后才替换正式目录。

    这样导入失败时不会留下半成品包，也不会让 load_packs 读到坏数据。
    """
    if not _UNIT_ID.match(str(pack_id or "")):
        raise PackError("场景 id 不合法")
    os.makedirs(USER_PACKS_DIR, exist_ok=True)
    tmp = os.path.join(USER_PACKS_DIR, "." + pack_id + ".tmp")
    final = os.path.join(USER_PACKS_DIR, pack_id)
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.makedirs(os.path.join(tmp, "units"))
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        for unit in units:
            path = os.path.join(tmp, "units", str(unit["id"]) + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(unit, f, ensure_ascii=False, indent=2)
        _load_pack(tmp, True)                    # 完整校验，失败会抛 PackError
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    reset_cache()
    return final


def _user_pack_dir(pack_id):
    """只认 USER_PACKS_DIR 下的目录，内置包一律拒绝修改。"""
    pid = str(pack_id or "")
    if not _UNIT_ID.match(pid):
        return None
    d = os.path.join(USER_PACKS_DIR, pid)
    return d if os.path.isdir(d) else None


def delete_user_pack(pack_id):
    d = _user_pack_dir(pack_id)
    if not d:
        raise PackError("只能删除自己导入的场景")
    shutil.rmtree(d, ignore_errors=True)
    reset_cache()


def rename_user_pack(pack_id, name):
    name = str(name or "").strip()[:40]
    if not name:
        raise PackError("场景名不能为空")
    d = _user_pack_dir(pack_id)
    if not d:
        raise PackError("只能重命名自己导入的场景")
    manifest = read_json(os.path.join(d, "manifest.json"), None)
    if not isinstance(manifest, dict):
        raise PackError("manifest 读取失败")
    manifest = dict(manifest, name=name)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    reset_cache()
    return public_manifest(get_pack(pack_id))


def export_pack(pack_id):
    """导出成可分享的 JSON；内置包也允许导出。"""
    pack = get_pack(pack_id)
    return {
        "format": "dojo-pack",
        "version": 1,
        "manifest": pack["manifest"],
        "units": pack["units"],
    }


def _unique_pack_id(base):
    pid, i = base, 2
    alive = {p["manifest"]["id"] for p in load_packs()}
    while pid in alive or os.path.isdir(os.path.join(USER_PACKS_DIR, pid)):
        pid = "%s-%d" % (base[:58], i)
        i += 1
    return pid


def import_pack(payload):
    """导入他人分享的包：一律降级成 personal，并换一个不冲突的 id。"""
    if not isinstance(payload, dict):
        raise PackError("文件内容不是场景包")
    manifest = payload.get("manifest")
    units = payload.get("units")
    if not isinstance(manifest, dict) or not isinstance(units, list) or not units:
        raise PackError("场景包缺少 manifest 或 units")
    name = str(manifest.get("name") or "导入的场景").strip()[:40] or "导入的场景"
    clean_units = [u for u in units if isinstance(u, dict) and str(u.get("id") or "")]
    if not clean_units:
        raise PackError("场景包里没有有效单元")
    pid = _unique_pack_id(pack_slug(name))
    m = {
        "id": pid,
        "name": name,
        "desc": str(manifest.get("desc") or "导入的个人场景")[:200],
        "version": int(manifest.get("version") or 1),
        "grade": "personal",
        "alignment": manifest.get("alignment") or {"standard": "导入材料", "dimensions": ["自定义"]},
        "ability_weights": manifest.get("ability_weights") or {},
        "entry": {"minutes": 6, "first_unit": clean_units[0]["id"]},
        "sequence": [u["id"] for u in clean_units],
    }
    if not m["ability_weights"]:
        m["ability_weights"] = {"framing": 1}
    write_pack(pid, m, clean_units)
    return public_manifest(get_pack(pid))
