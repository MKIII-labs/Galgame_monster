# -*- coding: utf-8 -*-
"""3D 模型体检：OBJ / glTF / GLB 的结构化事实与错误处理。"""
import json
import struct

from app import mesh


def _write_glb(path, gltf):
    raw = json.dumps(gltf, ensure_ascii=False).encode("utf-8")
    pad = (4 - len(raw) % 4) % 4
    raw += b" " * pad
    total = 12 + 8 + len(raw)
    head = b"glTF" + struct.pack("<II", 2, total)
    chunk = struct.pack("<II", len(raw), 0x4E4F534A) + raw
    path.write_bytes(head + chunk)


def test_obj_basic_stats_and_bbox(tmp_path):
    p = tmp_path / "tri.obj"
    p.write_text(
        "# comment\n"
        "v 0 0 0\n v 2 0 0\n v 0 3 0\n v 0 0 4\n"
        "vt 0 0\n vn 0 0 1\n"
        "o Box\n usemtl Wood\n mtllib missing.mtl\n"
        "f 1/1/1 2/1/1 3/1/1\n"
        "f 1 2 3 4\n",
        encoding="utf-8")
    text, m = mesh.inspect(str(p))
    assert m["vertices"] == 4 and m["triangles"] == 3
    assert (m["bbox_x"], m["bbox_y"], m["bbox_z"]) == (2, 3, 4)
    assert m["objects"] == 1 and m["materials"] == 1
    assert m["missing_files"] == 1
    assert "__MESH_METRICS__" in text and "mtllib 找不到" in text


def test_obj_bad_faces_and_refs(tmp_path):
    p = tmp_path / "bad.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\n"
                 "f 1 2 9\n"      # 越界
                 "f 1 2\n",       # 不足三个点
                 encoding="utf-8")
    _text, m = mesh.inspect(str(p))
    assert m["triangles"] == 0
    assert m["degenerate"] == 2
    assert m["missing_files"] == 0


def test_gltf_counts_and_missing_texture(tmp_path):
    gltf = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": [10, 0, 0]}],
        "meshes": [{"primitives": [
            {"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2}},
        ]}],
        "accessors": [
            {"count": 6, "type": "VEC3", "min": [-1, 0, -2], "max": [1, 4, 2]},
            {"count": 6, "type": "VEC3"},
            {"count": 6, "type": "VEC2"},
        ],
        "materials": [{"name": "M"}],
        "textures": [{"source": 0}],
        "images": [{"uri": "textures/missing.png"}],
    }
    p = tmp_path / "scene.gltf"
    p.write_text(json.dumps(gltf), encoding="utf-8")
    text, m = mesh.inspect(str(p))
    assert m["format"] == "gltf"
    assert m["vertices"] == 6 and m["triangles"] == 2
    assert m["materials"] == 1 and m["textures"] == 1
    assert m["missing_textures"] == 1
    # 节点平移 10 后，世界包围盒 x 从 9 到 11（尺寸仍是 2）
    assert m["bbox_x"] == 2
    assert "有 1 个外部引用找不到" in text


def test_glb_parses_json_chunk(tmp_path):
    gltf = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [
            {"attributes": {"POSITION": 0}, "indices": 1, "mode": 4},
        ]}],
        "accessors": [
            {"count": 12, "type": "VEC3", "min": [-1, -1, -1], "max": [1, 1, 1]},
            {"count": 12, "type": "SCALAR"},
        ],
    }
    p = tmp_path / "scene.glb"
    _write_glb(p, gltf)
    _text, m = mesh.inspect(str(p))
    assert m["format"] == "glb"
    assert m["triangles"] == 4
    assert m["normals_missing"] == 1 and m["uv_missing"] == 1


def test_rejects_unknown_and_broken_files(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("x", encoding="utf-8")
    assert mesh.inspect(str(txt))[0].startswith("❌")
    assert mesh.inspect(str(tmp_path / "nope.glb"))[0].startswith("❌")
    bad = tmp_path / "bad.glb"
    bad.write_bytes(b"glTF" + struct.pack("<II", 2, 99) + b"\x00" * 8)
    assert mesh.inspect(str(bad))[0].startswith("❌")
