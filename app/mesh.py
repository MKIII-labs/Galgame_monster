# -*- coding: utf-8 -*-
"""3D 模型体检：把「模型说建好了」变成可断言的结构化事实。

只解析文件本身，不做渲染、不做拓扑修复、不做布尔运算：

- glTF / GLB：nodes / meshes / primitives / materials / textures / images、
  三角面与顶点数、包围盒（应用节点变换）、法线与 UV 缺失、贴图引用缺失；
- OBJ：v / vt / vn / f 计数、三角化后的三角面数、包围盒、对象 / 分组、
  mtllib 是否存在、非法面与越界索引；
- 统一输出 ``__MESH_METRICS__`` JSON，供 acceptance 直接断言。

限制：只认几何与文件引用，判断不了「好不好看」；非流形 / 自交 / 法线朝外
这类需要解码顶点与拓扑的检查不在本模块范围内（会明确标注「未检查」）。
"""
import json
import os
import struct

from app.assets import MODEL_EXTS

MAX_FILE = 64 * 1024 * 1024       # 单文件上限：再大就别塞进上下文了
MAX_LIST = 8                      # 明细列表最多列几项

GLB_MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A


def _num(v):
    """把 float 收敛成便于阅读的数字（整数就去掉 .0）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    if abs(f - round(f)) < 1e-9:
        return int(round(f))
    return round(f, 4)


def _fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return "%.1fGB" % n


def _join(path, uri):
    """把 glTF / OBJ 里的相对引用拼成绝对路径；data: / http: 不当作本地文件。"""
    u = str(uri or "").strip()
    if not u or u.startswith(("data:", "http://", "https://", "blob:")):
        return ""
    u = u.replace("\\", "/")
    return os.path.normpath(os.path.join(os.path.dirname(path), u))


def _missing_refs(path, uris):
    out = []
    for u in uris:
        full = _join(path, u)
        if full and not os.path.isfile(full):
            out.append(str(u))
    return out[:MAX_LIST]


# ---------------------------------------------------------------- glTF / GLB

def _read_glb(path):
    """解析 GLB 容器，返回 JSON dict（只读 JSON chunk，不读二进制数据）。"""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != GLB_MAGIC:
            raise ValueError("不是合法的 GLB（magic 不匹配）")
        _ver, total = struct.unpack("<II", head[4:12])
        got = 12
        while got < total:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            clen, ctype = struct.unpack("<II", hdr)
            body = f.read(clen)
            got += 8 + clen
            if ctype == CHUNK_JSON:
                return json.loads(body.decode("utf-8", "replace"))
    raise ValueError("GLB 里没有 JSON chunk")


def _read_json(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("glTF 根节点不是对象")
    return data


def _mat_mul(a, b):
    """4×4 矩阵乘法（列主序，与 glTF 一致）。"""
    out = [0.0] * 16
    for c in range(4):
        for r in range(4):
            out[c * 4 + r] = sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4))
    return out


def _mat_apply(m, p):
    x, y, z = p
    return (m[0] * x + m[4] * y + m[8] * z + m[12],
            m[1] * x + m[5] * y + m[9] * z + m[13],
            m[2] * x + m[6] * y + m[10] * z + m[14])


def _trs_matrix(node):
    if isinstance(node.get("matrix"), list) and len(node["matrix"]) == 16:
        try:
            return [float(x) for x in node["matrix"]]
        except (TypeError, ValueError):
            pass
    tx, ty, tz = (list(node.get("translation") or [0, 0, 0]) + [0, 0, 0])[:3]
    qx, qy, qz, qw = (list(node.get("rotation") or [0, 0, 0, 1]) + [0, 0, 0, 1])[:4]
    sx, sy, sz = (list(node.get("scale") or [1, 1, 1]) + [1, 1, 1])[:3]
    # 四元数 → 旋转矩阵（列主序）
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    r = [
        1 - 2 * (yy + zz), 2 * (xy + wz), 2 * (xz - wy), 0,
        2 * (xy - wz), 1 - 2 * (xx + zz), 2 * (yz + wx), 0,
        2 * (xz + wy), 2 * (yz - wx), 1 - 2 * (xx + yy), 0,
        0, 0, 0, 1,
    ]
    s = [sx, 0, 0, 0, 0, sy, 0, 0, 0, 0, sz, 0, 0, 0, 0, 1]
    t = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, tx, ty, tz, 1]
    return _mat_mul(t, _mat_mul(r, s))


def _prim_triangles(prim, acc):
    """一个 primitive 的三角面数：优先用 indices，其次用 POSITION 数量。"""
    mode = prim.get("mode")
    mode = 4 if mode is None else mode
    idx = prim.get("indices")
    n = 0
    if isinstance(idx, int) and 0 <= idx < len(acc):
        try:
            n = int(acc[idx].get("count") or 0)
        except (TypeError, ValueError):
            n = 0
    if not n:
        pos = (prim.get("attributes") or {}).get("POSITION")
        if isinstance(pos, int) and 0 <= pos < len(acc):
            try:
                n = int(acc[pos].get("count") or 0)
            except (TypeError, ValueError):
                n = 0
    if mode == 4:                     # TRIANGLES
        return n // 3
    if mode in (5, 6):                # STRIP / FAN
        return max(0, n - 2)
    return 0                          # POINTS / LINES


def _inspect_gltf(path, data, units=""):
    nodes = data.get("nodes") or []
    meshes = data.get("meshes") or []
    acc = data.get("accessors") or []
    mats = data.get("materials") or []
    textures = data.get("textures") or []
    images = data.get("images") or []
    warnings, missing_tex = [], []
    vertices = triangles = primitives = 0
    normals_missing = uv_missing = degenerate = 0
    bbox_min = [float("inf")] * 3
    bbox_max = [float("-inf")] * 3
    has_box = False

    # 每个 mesh 的局部包围盒（POSITION accessor 的 min/max）
    local_box = {}
    for mi, mesh in enumerate(meshes):
        mn, mx = [float("inf")] * 3, [float("-inf")] * 3
        ok = False
        for prim in (mesh.get("primitives") or []):
            primitives += 1
            if not isinstance(prim, dict):
                continue
            attr = prim.get("attributes") or {}
            pos = attr.get("POSITION")
            if not isinstance(pos, int) or not (0 <= pos < len(acc)):
                degenerate += 1
                continue
            a = acc[pos] if isinstance(acc[pos], dict) else {}
            try:
                vertices += int(a.get("count") or 0)
            except (TypeError, ValueError):
                pass
            tri = _prim_triangles(prim, acc)
            triangles += tri
            if tri <= 0:
                degenerate += 1
            if "NORMAL" not in attr:
                normals_missing += 1
            if "TEXCOORD_0" not in attr:
                uv_missing += 1
            amin, amax = a.get("min"), a.get("max")
            if (isinstance(amin, list) and len(amin) >= 3
                    and isinstance(amax, list) and len(amax) >= 3):
                for k in range(3):
                    mn[k] = min(mn[k], float(amin[k]))
                    mx[k] = max(mx[k], float(amax[k]))
                ok = True
        if ok:
            local_box[mi] = (mn, mx)

    # 节点变换：把每个 mesh 的局部包围盒变换到世界空间
    visited = set()

    def _walk(ni, parent):
        nonlocal has_box
        if not isinstance(ni, int) or not (0 <= ni < len(nodes)) or ni in visited:
            return
        visited.add(ni)
        node = nodes[ni] if isinstance(nodes[ni], dict) else {}
        world = _mat_mul(parent, _trs_matrix(node))
        mi = node.get("mesh")
        if isinstance(mi, int) and mi in local_box:
            mn, mx = local_box[mi]
            corners = [(x, y, z) for x in (mn[0], mx[0])
                       for y in (mn[1], mx[1]) for z in (mn[2], mx[2])]
            for c in corners:
                p = _mat_apply(world, c)
                for k in range(3):
                    bbox_min[k] = min(bbox_min[k], p[k])
                    bbox_max[k] = max(bbox_max[k], p[k])
                has_box = True
        for child in (node.get("children") or []):
            _walk(child, world)

    identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    scenes = data.get("scenes") or []
    roots = []
    for sc in scenes:
        roots += list(sc.get("nodes") or []) if isinstance(sc, dict) else []
    if not roots:
        referenced = {n.get("mesh") for n in nodes if isinstance(n, dict)}
        roots = [i for i, n in enumerate(nodes)
                 if isinstance(n, dict) and n.get("mesh") in referenced]
        if not roots:
            roots = list(range(min(len(nodes), 64)))
    for r in roots:
        _walk(r, identity)
    if not has_box:                   # 没有节点 / 没有 min-max：退回局部包围盒
        for mn, mx in local_box.values():
            for k in range(3):
                bbox_min[k] = min(bbox_min[k], mn[k])
                bbox_max[k] = max(bbox_max[k], mx[k])
            has_box = True

    uris = [img.get("uri") for img in images if isinstance(img, dict)]
    uris += [b.get("uri") for b in (data.get("buffers") or [])
             if isinstance(b, dict)]
    missing = _missing_refs(path, uris)
    missing_tex = missing
    if missing_tex:
        warnings.append("有 %d 个外部引用找不到" % len(missing_tex))
    if normals_missing:
        warnings.append("%d 个 primitive 没有 NORMAL（光照可能发黑/错误）" % normals_missing)
    if uv_missing:
        warnings.append("%d 个 primitive 没有 TEXCOORD_0（贴图无法定位）" % uv_missing)
    if degenerate:
        warnings.append("%d 个 primitive 没有可用的三角面" % degenerate)
    if data.get("extensionsRequired"):
        warnings.append("依赖扩展：" + "、".join(
            str(x) for x in (data.get("extensionsRequired") or [])[:MAX_LIST]))

    size = {}
    if has_box:
        size = {"x": _num(bbox_max[0] - bbox_min[0]),
                "y": _num(bbox_max[1] - bbox_min[1]),
                "z": _num(bbox_max[2] - bbox_min[2])}

    metrics = {
        "format": "glb" if path.lower().endswith(".glb") else "gltf",
        "bytes": os.path.getsize(path),
        "nodes": len(nodes), "meshes": len(meshes), "primitives": primitives,
        "vertices": vertices, "triangles": triangles,
        "materials": len(mats), "textures": len(textures), "images": len(images),
        "animations": len(data.get("animations") or []),
        "skins": len(data.get("skins") or []),
        "degenerate": degenerate,
        "missing_textures": len(missing_tex),
        "missing_files": len(missing),
        "normals_missing": normals_missing,
        "uv_missing": uv_missing,
    }
    if size:
        metrics.update({"bbox_x": size["x"], "bbox_y": size["y"],
                        "bbox_z": size["z"]})
    lines = [
        "【模型体检】%s · %s · %s" % (
            os.path.basename(path), "GLB" if metrics["format"] == "glb" else "glTF",
            _fmt_size(metrics["bytes"])),
        "结构：%d 个节点 · %d 个 mesh · %d 个 primitive · %d 个材质 · %d 张纹理"
        % (len(nodes), len(meshes), primitives, len(mats), len(textures)),
        "几何：%s 个三角面 · %s 个顶点%s"
        % (_num(triangles), _num(vertices),
           (" · bbox %s×%s×%s" % (size["x"], size["y"], size["z"])) if size else ""),
    ]
    if data.get("animations"):
        lines.append("动画：%d 段 · 蒙皮 %d 个"
                     % (len(data.get("animations") or []), len(data.get("skins") or [])))
    if warnings:
        lines.append("注意：" + "；".join(warnings[:MAX_LIST]))
    else:
        lines.append("注意：没有发现结构性问题（未检查法线朝向 / 自交 / 非流形）")
    lines.append("单位：%s" % (units or ("米（glTF 约定）" if metrics["format"] == "gltf"
                                       else "米（GLB 约定）")))
    lines.append("__MESH_METRICS__ " + json.dumps(metrics, ensure_ascii=False))
    return "\n".join(lines), metrics


# ---------------------------------------------------------------- OBJ

def _parse_index(tok, total, kind):
    """OBJ 的面索引：正数从 1 开始，负数相对末尾；0 / 越界视为非法。"""
    try:
        i = int(tok)
    except (TypeError, ValueError):
        return 0, kind + "索引不是整数"
    if i < 0:
        i = total + i + 1
    if i <= 0 or i > total:
        return 0, kind + "索引越界"
    return i, ""


def _inspect_obj(path, units=""):
    vertices = texcoords = normals = faces = 0
    triangles = bad_faces = bad_refs = degenerate = 0
    objects, groups, materials, mtllibs = [], [], [], []
    mn, mx = [float("inf")] * 3, [float("-inf")] * 3
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            tag = parts[0]
            if tag == "v" and len(parts) >= 4:
                try:
                    p = [float(parts[1]), float(parts[2]), float(parts[3])]
                except ValueError:
                    bad_faces += 1
                    continue
                vertices += 1
                for k in range(3):
                    mn[k] = min(mn[k], p[k])
                    mx[k] = max(mx[k], p[k])
            elif tag == "vt":
                texcoords += 1
            elif tag == "vn":
                normals += 1
            elif tag == "f":
                faces += 1
                toks = parts[1:]
                if len(toks) < 3:
                    bad_faces += 1
                    continue
                idxs = []
                bad = False
                for t in toks:
                    head = t.split("/")[0]
                    if not head:
                        bad = True
                        break
                    i, err = _parse_index(head, vertices, "顶点")
                    if err:
                        bad_refs += 1
                        bad = True
                        break
                    idxs.append(i)
                if bad:
                    continue
                if len(set(idxs)) < 3:
                    degenerate += 1
                    continue
                triangles += len(idxs) - 2
            elif tag == "o" and len(parts) > 1:
                objects.append(" ".join(parts[1:])[:60])
            elif tag == "g" and len(parts) > 1:
                groups.append(" ".join(parts[1:])[:60])
            elif tag == "usemtl" and len(parts) > 1:
                materials.append(" ".join(parts[1:])[:60])
            elif tag == "mtllib" and len(parts) > 1:
                mtllibs.append(" ".join(parts[1:]))

    missing = _missing_refs(path, mtllibs)
    warnings = []
    if bad_faces:
        warnings.append("%d 行面数据格式错误" % bad_faces)
    if bad_refs:
        warnings.append("%d 个面引用了不存在的顶点" % bad_refs)
    if degenerate:
        warnings.append("%d 个面退化成一条线 / 一个点" % degenerate)
    if missing:
        warnings.append("mtllib 找不到：%s" % "、".join(missing[:3]))
    if not texcoords:
        warnings.append("没有 UV（vt）")

    size = {}
    if vertices:
        size = {"x": _num(mx[0] - mn[0]), "y": _num(mx[1] - mn[1]),
                "z": _num(mx[2] - mn[2])}
    metrics = {
        "format": "obj", "bytes": os.path.getsize(path),
        "vertices": vertices, "triangles": triangles,
        "faces": faces, "objects": len(objects), "groups": len(groups),
        "materials": len(set(materials)), "textures": 0, "images": 0,
        "normals": normals, "uvs": texcoords,
        "degenerate": degenerate + bad_faces + bad_refs,
        "missing_textures": 0, "missing_files": len(missing),
        "normals_missing": 0 if normals else 1,
        "uv_missing": 0 if texcoords else 1,
    }
    if size:
        metrics.update({"bbox_x": size["x"], "bbox_y": size["y"],
                        "bbox_z": size["z"]})
    lines = [
        "【模型体检】%s · OBJ · %s" % (os.path.basename(path),
                                      _fmt_size(metrics["bytes"])),
        "几何：%s 个三角面 · %s 个顶点 · %s 个面（原始）%s"
        % (_num(triangles), _num(vertices), _num(faces),
           (" · bbox %s×%s×%s" % (size["x"], size["y"], size["z"])) if size else ""),
        "属性：UV %d · 法线 %d · 材质 %d · 对象 %d · 分组 %d"
        % (texcoords, normals, metrics["materials"], len(objects), len(groups)),
    ]
    if warnings:
        lines.append("注意：" + "；".join(warnings[:MAX_LIST]))
    else:
        lines.append("注意：没有发现结构性问题（未检查非流形 / 自交 / 法线朝向）")
    lines.append("单位：%s" % (units or "未指定（OBJ 本身没有单位）"))
    lines.append("__MESH_METRICS__ " + json.dumps(metrics, ensure_ascii=False))
    return "\n".join(lines), metrics


# ---------------------------------------------------------------- 入口

def inspect(path, units=""):
    """体检一个 3D 模型文件；返回 (给模型看的文本, 指标 dict)。"""
    rel = str(path or "")
    if not rel:
        return "❌ mesh_inspect 需要 path。", {}
    if not os.path.isfile(rel):
        return "❌ 文件不存在：%s" % rel, {}
    ext = os.path.splitext(rel)[1].lower()
    if ext not in MODEL_EXTS:
        return ("❌ mesh_inspect 只支持 %s（当前：%s）"
                % (" / ".join(sorted(MODEL_EXTS)), ext or "无扩展名")), {}
    if ext == ".mtl":
        return "❌ .mtl 是材质文件，不是模型；请传 .obj / .gltf / .glb。", {}
    if os.path.getsize(rel) > MAX_FILE:
        return ("❌ 文件太大（%s），超过体检上限 %s。"
                % (_fmt_size(os.path.getsize(rel)), _fmt_size(MAX_FILE))), {}
    try:
        if ext == ".glb":
            return _inspect_gltf(rel, _read_glb(rel), units=units)
        if ext == ".gltf":
            return _inspect_gltf(rel, _read_json(rel), units=units)
        return _inspect_obj(rel, units=units)
    except ValueError as e:
        return "❌ 模型文件解析失败：%s" % e, {}
    except (OSError, struct.error) as e:
        return "❌ 读取模型失败：%s" % e, {}
    except Exception as e:                     # noqa: BLE001  坏文件不能拖垮主流程
        return "❌ 模型体检失败：%s" % e, {}
