"""Deterministic ``street.glb`` writer (spec/portability.md section 3, slice S7).

``write_glb(mesh)`` turns a :class:`~hero.export.mesh_types.StreetMesh` into glTF 2.0 binary bytes. Determinism is
**not** delegated to a library: the binary chunk is packed with ``struct`` (little-endian, 4-byte aligned, fixed buffer
order), the JSON chunk is serialised from an insertion-ordered dict with fixed separators, and the 12-byte header and
both chunk headers are written here. ``pygltflib`` is used only to re-load / model the result (:func:`read_glb`).

Layout (the order is part of the contract; same input => same bytes in one environment):
  nodes[0] = root ``Street`` (children = the mesh nodes, in ``mesh.nodes`` order); per node: POSITION, NORMAL,
  [TEXCOORD_0], [COLOR_0], indices; then one bufferView per texture (JPEG, in ``mesh.textures`` order).

Colour spaces: ``baseColorFactor`` is **linear** (glTF spec), so the sRGB hex of a ``MeshMaterial`` is converted with the
sRGB EOTF. ``COLOR_0`` bytes are written exactly as given by the mesh (the builder decides what they mean).
"""

from __future__ import annotations

import json
import math
import struct
from typing import Any

from hero.export import GENERATOR
from hero.export.mesh_types import ROOT_NODE, SCENE_NAME, MeshNode, StreetMesh

MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942

# glTF constants
FLOAT, UBYTE, USHORT, UINT = 5126, 5121, 5123, 5125
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963
SAMPLER = {"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}  # LINEAR, mipmap, CLAMP_TO_EDGE


class GlbError(ValueError):
    """The mesh cannot be serialised (bad indices, NaN, missing texture...)."""


def srgb_to_linear(c: int) -> float:
    x = c / 255.0
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    if len(h) != 7 or h[0] != "#":
        raise GlbError(f"bad colour {h!r} (expected #rrggbb)")
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def base_color_factor(hex_srgb: str) -> list[float]:
    """sRGB ``#rrggbb`` -> linear RGBA ``baseColorFactor`` (6 dp)."""
    return [round(srgb_to_linear(c), 6) for c in hex_to_rgb(hex_srgb)] + [1.0]


def _f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


def _flat(rows: list[tuple[float, ...]], width: int, what: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        if len(r) != width:
            raise GlbError(f"{what}: expected {width} components, got {len(r)}")
        out.extend(r)
    if not all(math.isfinite(v) for v in out):
        raise GlbError(f"{what}: NaN/inf in attribute data")
    return out


class _Buf:
    def __init__(self) -> None:
        self.data = bytearray()
        self.views: list[dict[str, Any]] = []
        self.accessors: list[dict[str, Any]] = []

    def view(self, data: bytes, target: int | None) -> int:
        while len(self.data) % 4:
            self.data.append(0)
        v: dict[str, Any] = {"buffer": 0, "byteOffset": len(self.data), "byteLength": len(data)}
        if target:
            v["target"] = target
        self.data.extend(data)
        self.views.append(v)
        return len(self.views) - 1

    def accessor(self, view: int, ctype: int, count: int, atype: str, **kw: Any) -> int:
        a: dict[str, Any] = {"bufferView": view, "componentType": ctype, "count": count, "type": atype}
        a.update(kw)
        self.accessors.append(a)
        return len(self.accessors) - 1


def _mesh_node(buf: _Buf, nd: MeshNode, mat_index: dict[str, int]) -> dict[str, Any]:
    nv = len(nd.positions)
    if nv == 0 or not nd.indices:
        raise GlbError(f"node {nd.name}: no geometry (nodes without geometry must be omitted by the builder)")
    if len(nd.normals) != nv:
        raise GlbError(f"node {nd.name}: {len(nd.normals)} normals for {nv} positions")
    if nd.uvs is not None and len(nd.uvs) != nv:
        raise GlbError(f"node {nd.name}: {len(nd.uvs)} uvs for {nv} positions")
    if nd.colors is not None and len(nd.colors) != nv:
        raise GlbError(f"node {nd.name}: {len(nd.colors)} colours for {nv} positions")
    if len(nd.indices) % 3:
        raise GlbError(f"node {nd.name}: index count {len(nd.indices)} is not a multiple of 3")
    if min(nd.indices) < 0 or max(nd.indices) >= nv:
        raise GlbError(f"node {nd.name}: index out of range [0, {nv})")
    if nd.material not in mat_index:
        raise GlbError(f"node {nd.name}: unknown material {nd.material!r}")

    attrs: dict[str, int] = {}
    pos = _flat(nd.positions, 3, f"{nd.name}.POSITION")
    pos = [_f32(v) for v in pos]  # min/max must equal the stored float32 values
    mins = [min(pos[k::3]) for k in range(3)]
    maxs = [max(pos[k::3]) for k in range(3)]
    v = buf.view(struct.pack(f"<{len(pos)}f", *pos), ARRAY_BUFFER)
    attrs["POSITION"] = buf.accessor(v, FLOAT, nv, "VEC3", min=mins, max=maxs)
    nrm = _flat(nd.normals, 3, f"{nd.name}.NORMAL")
    v = buf.view(struct.pack(f"<{len(nrm)}f", *nrm), ARRAY_BUFFER)
    attrs["NORMAL"] = buf.accessor(v, FLOAT, nv, "VEC3")
    if nd.uvs is not None:
        uv = _flat(nd.uvs, 2, f"{nd.name}.TEXCOORD_0")
        v = buf.view(struct.pack(f"<{len(uv)}f", *uv), ARRAY_BUFFER)
        attrs["TEXCOORD_0"] = buf.accessor(v, FLOAT, nv, "VEC2")
    if nd.colors is not None:
        cols: list[int] = []
        for c in nd.colors:
            if len(c) != 4 or not all(0 <= int(x) <= 255 for x in c):
                raise GlbError(f"node {nd.name}: COLOR_0 entries must be 4 ints in 0..255, got {c!r}")
            cols.extend(int(x) for x in c)
        v = buf.view(struct.pack(f"<{len(cols)}B", *cols), ARRAY_BUFFER)
        attrs["COLOR_0"] = buf.accessor(v, UBYTE, nv, "VEC4", normalized=True)
    if nv >= 65535:
        fmt, ctype = "I", UINT
    else:
        fmt, ctype = "H", USHORT
    v = buf.view(struct.pack(f"<{len(nd.indices)}{fmt}", *nd.indices), ELEMENT_ARRAY_BUFFER)
    ia = buf.accessor(v, ctype, len(nd.indices), "SCALAR", min=[min(nd.indices)], max=[max(nd.indices)])
    return {"name": nd.name, "primitives": [{"attributes": attrs, "indices": ia, "material": mat_index[nd.material], "mode": 4}]}


def build_document(mesh: StreetMesh) -> tuple[dict[str, Any], bytes]:
    """Return ``(gltf_json_dict, binary_chunk_payload_unpadded)``; the dict is insertion-ordered (key order is fixed)."""
    if not mesh.nodes:
        raise GlbError("mesh has no nodes")
    names = [n.name for n in mesh.nodes]
    if len(set(names)) != len(names):
        raise GlbError("duplicate node names")
    buf = _Buf()
    mat_index = {m.name: i for i, m in enumerate(mesh.materials)}
    if len(mat_index) != len(mesh.materials):
        raise GlbError("duplicate material names")
    meshes: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = [{"name": ROOT_NODE, "children": list(range(1, len(mesh.nodes) + 1))}]
    for nd in mesh.nodes:
        meshes.append(_mesh_node(buf, nd, mat_index))
        nodes.append({"name": nd.name, "mesh": len(meshes) - 1, "extras": nd.extras})

    images: list[dict[str, Any]] = []
    textures: list[dict[str, Any]] = []
    tex_index: dict[str, int] = {}
    for name, data in mesh.textures.items():
        if not data.startswith(b"\xff\xd8\xff"):
            raise GlbError(f"texture {name}: not a JPEG")
        v = buf.view(data, None)
        images.append({"name": name, "mimeType": "image/jpeg", "bufferView": v})
        textures.append({"sampler": 0, "source": len(images) - 1, "name": name})
        tex_index[name] = len(textures) - 1
    materials: list[dict[str, Any]] = []
    for m in mesh.materials:
        pbr: dict[str, Any] = {"baseColorFactor": base_color_factor(m.base_color_hex)}
        if m.texture:
            if m.texture not in tex_index:
                raise GlbError(f"material {m.name}: texture {m.texture!r} not in mesh.textures")
            pbr["baseColorTexture"] = {"index": tex_index[m.texture]}
        pbr["metallicFactor"] = m.metallic
        pbr["roughnessFactor"] = m.roughness
        materials.append({"name": m.name, "pbrMetallicRoughness": pbr, "doubleSided": False, "alphaMode": "OPAQUE"})

    doc: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": GENERATOR, "extras": mesh.asset_extras},
        "scene": 0,
        "scenes": [{"name": SCENE_NAME, "nodes": [0]}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
    }
    if textures:
        doc["textures"] = textures
        doc["images"] = images
        doc["samplers"] = [dict(SAMPLER)]
    doc["accessors"] = buf.accessors
    doc["bufferViews"] = buf.views
    doc["buffers"] = [{"byteLength": len(buf.data)}]
    return doc, bytes(buf.data)


def pack_glb(doc: dict[str, Any], binary: bytes) -> bytes:
    """Assemble header + JSON chunk (space padded) + BIN chunk (zero padded). Pure function of its arguments."""
    js = json.dumps(doc, sort_keys=False, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    js += b" " * (-len(js) % 4)
    body = binary + b"\x00" * (-len(binary) % 4)
    total = 12 + 8 + len(js) + 8 + len(body)
    return (
        struct.pack("<4sII", MAGIC, 2, total)
        + struct.pack("<II", len(js), CHUNK_JSON) + js
        + struct.pack("<II", len(body), CHUNK_BIN) + body
    )


def write_glb(mesh: StreetMesh) -> bytes:
    """Serialise ``mesh`` to glTF 2.0 binary bytes. Deterministic; raises :class:`GlbError` on invalid input."""
    doc, binary = build_document(mesh)
    return pack_glb(doc, binary)


def split_glb(data: bytes) -> tuple[dict[str, Any], bytes]:
    """Parse the container only (no pygltflib): ``(json_dict, bin_chunk)``. Raises :class:`GlbError` if malformed."""
    if len(data) < 20:
        raise GlbError("file shorter than a glb header + chunk header")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != MAGIC:
        raise GlbError(f"bad magic {magic!r}")
    if version != 2:
        raise GlbError(f"glb container version {version} != 2")
    if length != len(data):
        raise GlbError(f"header length {length} != file size {len(data)}")
    off = 12
    chunks: list[tuple[int, bytes]] = []
    while off < len(data):
        if off + 8 > len(data):
            raise GlbError("truncated chunk header")
        clen, ctype = struct.unpack_from("<II", data, off)
        off += 8
        if off + clen > len(data):
            raise GlbError("chunk overruns file")
        chunks.append((ctype, data[off:off + clen]))
        off += clen
    if not chunks or chunks[0][0] != CHUNK_JSON:
        raise GlbError("first chunk is not JSON")
    try:
        doc = json.loads(chunks[0][1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GlbError(f"JSON chunk invalid: {e}") from e
    binary = chunks[1][1] if len(chunks) > 1 and chunks[1][0] == CHUNK_BIN else b""
    return doc, binary


def read_glb(data: bytes):  # -> pygltflib.GLTF2
    """Re-load the bytes with ``pygltflib`` (round-trip / structural model; never used to emit bytes)."""
    from pygltflib import GLTF2

    g = GLTF2.load_from_bytes(data)
    if g is None:
        raise GlbError("pygltflib could not load the glb")
    return g
