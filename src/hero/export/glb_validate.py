"""Structural + contract validation of ``street.glb`` (spec/portability.md section 3, slice S7).

Independent of the writer: it parses the container and the accessors itself (``struct``), so it also validates glbs
produced by anything else (the stand-in fixture, a future exporter). Checks:

  container     magic / version / length / chunk order / 4-byte alignment / buffer length
  accessors     bufferView ranges, offset alignment, component types, POSITION min/max equal the data,
                index range, triangle multiple of 3, ushort vs uint rule, NORMAL unit length, UV in [0, 1]
  references    material / texture / image / sampler / bufferView indices, JPEG magic, no required extensions
  contract      node-name set, one mesh + one primitive per node, identity transforms, material per node,
                attribute set per node (TEXCOORD_0 / COLOR_0), extras shape, entity/segment index ranges,
                contiguous frontage, budgets (primitives / triangles / materials / textures / bytes)
  pygltflib     the bytes re-load with pygltflib

``validate_glb(data)`` returns a :class:`GlbReport`; ``khronos_validate(path)`` runs the Khronos reference validator
through ``npx`` when available (returns ``None`` when it cannot be run, with the reason in ``KhronosResult``).
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hero.export.glb import FLOAT, UBYTE, UINT, USHORT, GlbError, read_glb, split_glb
from hero.export.mesh_types import (
    ASSET_SCHEMA, FRONTAGE_NODES, FURNITURE_CATEGORY, FURNITURE_NODES, GROUND_NODES, MATERIAL_NAMES, MAX_GLB_BYTES,
    MAX_GLB_BYTES_200M, MAX_MATERIALS, MAX_PRIMITIVES, MAX_TEXTURES, MAX_TRIANGLES, NODE_HAS_UV, NODE_MATERIAL,
    NODE_NAMES, ROOT_NODE, SCENE_NAME,
)

KHRONOS_PACKAGE = "gltf-validator@2.0.0-dev.3.10"
_COMP = {FLOAT: ("f", 4), UBYTE: ("B", 1), USHORT: ("H", 2), UINT: ("I", 4), 5120: ("b", 1), 5122: ("h", 2)}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_ALLOWED_ATTRS = {"POSITION", "NORMAL", "TEXCOORD_0", "COLOR_0"}
UV_EPS = 1e-4


@dataclass
class GlbReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "stats": self.stats}


class _Ctx:
    def __init__(self, doc: dict[str, Any], binary: bytes, rep: GlbReport) -> None:
        self.doc, self.bin, self.rep = doc, binary, rep
        self._cache: dict[int, list[tuple] | None] = {}

    def err(self, msg: str) -> None:
        self.rep.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.rep.warnings.append(msg)

    def lst(self, key: str) -> list[Any]:
        v = self.doc.get(key, [])
        return v if isinstance(v, list) else []

    def read_accessor(self, idx: int, where: str) -> list[tuple] | None:
        """Decode an accessor to a list of tuples (None + error when it is malformed)."""
        if idx in self._cache:
            return self._cache[idx]
        out = self._read_accessor(idx, where)
        self._cache[idx] = out
        return out

    def _read_accessor(self, idx: int, where: str) -> list[tuple] | None:
        accs, views = self.lst("accessors"), self.lst("bufferViews")
        if not isinstance(idx, int) or not 0 <= idx < len(accs):
            self.err(f"{where}: accessor index {idx!r} out of range")
            return None
        a = accs[idx]
        ct, at, count = a.get("componentType"), a.get("type"), a.get("count")
        if ct not in _COMP or at not in _NCOMP or not isinstance(count, int) or count <= 0:
            self.err(f"{where}: accessor {idx} has bad componentType/type/count ({ct!r}/{at!r}/{count!r})")
            return None
        fmt, csize = _COMP[ct]
        n = _NCOMP[at]
        bv = a.get("bufferView")
        if not isinstance(bv, int) or not 0 <= bv < len(views):
            self.err(f"{where}: accessor {idx} bufferView {bv!r} out of range")
            return None
        v = views[bv]
        if v.get("buffer", 0) != 0:
            self.err(f"{where}: bufferView {bv} references buffer {v.get('buffer')} (only buffer 0 exists)")
            return None
        voff, vlen = v.get("byteOffset", 0), v.get("byteLength", 0)
        aoff = a.get("byteOffset", 0)
        stride = v.get("byteStride") or n * csize
        if aoff % csize:
            self.err(f"{where}: accessor {idx} byteOffset {aoff} not aligned to component size {csize}")
        if (voff + aoff) % 4:
            self.err(f"{where}: accessor {idx} absolute offset {voff + aoff} is not 4-byte aligned")
        need = aoff + stride * (count - 1) + n * csize
        if need > vlen:
            self.err(f"{where}: accessor {idx} needs {need} bytes but bufferView {bv} has {vlen}")
            return None
        if voff + vlen > len(self.bin):
            self.err(f"{where}: bufferView {bv} [{voff}, {voff + vlen}) exceeds the binary chunk ({len(self.bin)})")
            return None
        rows = []
        base = voff + aoff
        sfmt = "<" + fmt * n
        for i in range(count):
            rows.append(struct.unpack_from(sfmt, self.bin, base + i * stride))
        self._check_minmax(a, idx, rows, n, ct, where)
        return rows

    def _check_minmax(self, a: dict[str, Any], idx: int, rows: list[tuple], n: int, ct: int, where: str) -> None:
        mn, mx = a.get("min"), a.get("max")
        if mn is None and mx is None:
            return
        if not (isinstance(mn, list) and isinstance(mx, list) and len(mn) == n and len(mx) == n):
            self.err(f"{where}: accessor {idx} min/max must both be lists of {n}")
            return
        for k in range(n):
            lo, hi = min(r[k] for r in rows), max(r[k] for r in rows)
            if ct == FLOAT:
                if not (mn[k] == lo and mx[k] == hi):
                    self.err(f"{where}: accessor {idx} min/max[{k}] = {mn[k]}/{mx[k]} but data is {lo}/{hi}")
            elif not (mn[k] == lo and mx[k] == hi):
                self.err(f"{where}: accessor {idx} min/max[{k}] = {mn[k]}/{mx[k]} but data is {lo}/{hi}")


# ---------------------------------------------------------------------------------------------------------------------


def validate_glb(data: bytes, *, contract: bool = True, budgets: bool = True) -> GlbReport:
    """Validate glb bytes. ``contract=False`` checks only generic glTF structure (any glb)."""
    rep = GlbReport()
    try:
        doc, binary = split_glb(data)
    except GlbError as e:
        rep.errors.append(f"container: {e}")
        return rep
    c = _Ctx(doc, binary, rep)
    _container(c, data)
    _top_level(c)
    _meshes(c)
    _materials(c)
    try:
        read_glb(data)
    except Exception as e:  # noqa: BLE001 - any loader failure is a validation error
        c.err(f"pygltflib failed to load the glb: {e}")
    if contract:
        _contract(c)
    if budgets and contract:
        _budgets(c, len(data))
    return rep


def validate_glb_file(path: str | Path, **kw: Any) -> GlbReport:
    return validate_glb(Path(path).read_bytes(), **kw)


def _container(c: _Ctx, data: bytes) -> None:
    jlen = struct.unpack_from("<I", data, 12)[0]
    if jlen % 4:
        c.err(f"container: JSON chunk length {jlen} is not 4-byte aligned")
    if len(c.bin) % 4:
        c.err(f"container: BIN chunk length {len(c.bin)} is not 4-byte aligned")
    bufs = c.lst("buffers")
    if len(bufs) != 1:
        c.err(f"container: expected exactly 1 buffer, found {len(bufs)}")
        return
    bl = bufs[0].get("byteLength", -1)
    if "uri" in bufs[0]:
        c.err("container: buffer must not have a uri in a glb")
    if not (len(c.bin) - 3 <= bl <= len(c.bin)):
        c.err(f"container: buffer.byteLength {bl} inconsistent with BIN chunk length {len(c.bin)}")


def _top_level(c: _Ctx) -> None:
    d = c.doc
    if d.get("asset", {}).get("version") != "2.0":
        c.err("asset.version must be '2.0'")
    if d.get("extensionsRequired"):
        c.err(f"extensionsRequired must be empty, found {d['extensionsRequired']}")
    if d.get("extensionsUsed"):
        c.warn(f"extensionsUsed present: {d['extensionsUsed']}")
    views = c.lst("bufferViews")
    nbin = len(c.bin)
    for i, v in enumerate(views):
        off, ln = v.get("byteOffset", 0), v.get("byteLength", 0)
        if off < 0 or ln <= 0 or off + ln > nbin:
            c.err(f"bufferView {i} range [{off}, {off + ln}) outside the binary chunk (size {nbin})")
        if v.get("target") in (34962, 34963) and off % 4:
            c.err(f"bufferView {i} byteOffset {off} is not 4-byte aligned")
    scenes = c.lst("scenes")
    sc = d.get("scene")
    if not scenes or not isinstance(sc, int) or not 0 <= sc < len(scenes):
        c.err("scene index missing or out of range")
    nodes = c.lst("nodes")
    for si, s in enumerate(scenes):
        for n in s.get("nodes", []):
            if not isinstance(n, int) or not 0 <= n < len(nodes):
                c.err(f"scenes[{si}] references node {n!r} out of range")
    for ni, n in enumerate(nodes):
        for ch in n.get("children", []):
            if not isinstance(ch, int) or not 0 <= ch < len(nodes):
                c.err(f"nodes[{ni}] child {ch!r} out of range")
        if "mesh" in n and not 0 <= n["mesh"] < len(c.lst("meshes")):
            c.err(f"nodes[{ni}] mesh {n['mesh']!r} out of range")
    # images / textures / samplers
    imgs, texs, samplers = c.lst("images"), c.lst("textures"), c.lst("samplers")
    for i, im in enumerate(imgs):
        bv = im.get("bufferView")
        if "uri" in im or bv is None:
            c.err(f"images[{i}] must be embedded via bufferView")
            continue
        if not isinstance(bv, int) or not 0 <= bv < len(views):
            c.err(f"images[{i}] bufferView {bv!r} out of range")
            continue
        off = views[bv].get("byteOffset", 0)
        head = c.bin[off:off + 3]
        if im.get("mimeType") != "image/jpeg" or head != b"\xff\xd8\xff":
            c.err(f"images[{i}] is not a JPEG (mimeType {im.get('mimeType')!r}, magic {head!r})")
    for i, t in enumerate(texs):
        if not isinstance(t.get("source"), int) or not 0 <= t["source"] < len(imgs):
            c.err(f"textures[{i}] source {t.get('source')!r} out of range")
        if "sampler" in t and not 0 <= t["sampler"] < len(samplers):
            c.err(f"textures[{i}] sampler {t['sampler']!r} out of range")
    for i, s in enumerate(samplers):
        if s.get("wrapS", 10497) != 33071 or s.get("wrapT", 10497) != 33071:
            c.warn(f"samplers[{i}] is not CLAMP_TO_EDGE (spec requires it for the baked atlases)")


def _meshes(c: _Ctx) -> None:
    mats = c.lst("materials")
    for mi, m in enumerate(c.lst("meshes")):
        prims = m.get("primitives", [])
        for pi, p in enumerate(prims):
            where = f"meshes[{mi}]({m.get('name')}).primitives[{pi}]"
            if p.get("mode", 4) != 4:
                c.err(f"{where}: mode {p.get('mode')} is not TRIANGLES (4)")
            attrs = p.get("attributes", {})
            unknown = set(attrs) - _ALLOWED_ATTRS
            if unknown:
                c.err(f"{where}: attributes {sorted(unknown)} not in the contract (no tangents/skins/extra sets)")
            if "POSITION" not in attrs:
                c.err(f"{where}: POSITION missing")
                continue
            if "material" in p and not (isinstance(p["material"], int) and 0 <= p["material"] < len(mats)):
                c.err(f"{where}: material {p['material']!r} out of range")
            cols: dict[str, list[tuple] | None] = {}
            for name, (ct, at) in {"POSITION": (FLOAT, "VEC3"), "NORMAL": (FLOAT, "VEC3"), "TEXCOORD_0": (FLOAT, "VEC2"),
                                   "COLOR_0": (UBYTE, "VEC4")}.items():
                if name not in attrs:
                    continue
                aidx = attrs[name]
                accs = c.lst("accessors")
                if isinstance(aidx, int) and 0 <= aidx < len(accs):
                    a = accs[aidx]
                    if a.get("componentType") != ct or a.get("type") != at:
                        c.err(f"{where}: {name} must be {at} componentType {ct}, got {a.get('type')}/{a.get('componentType')}")
                    if name == "COLOR_0" and not a.get("normalized"):
                        c.err(f"{where}: COLOR_0 must be normalized")
                    if name == "POSITION" and ("min" not in a or "max" not in a):
                        c.err(f"{where}: POSITION accessor needs min and max")
                cols[name] = c.read_accessor(aidx, f"{where}.{name}")
            pos = cols.get("POSITION")
            nv = len(pos) if pos else 0
            if pos:
                for r in pos:
                    if not all(math.isfinite(x) for x in r):
                        c.err(f"{where}: non-finite POSITION")
                        break
            for name, rows in cols.items():
                if rows is not None and pos and len(rows) != nv:
                    c.err(f"{where}: {name} count {len(rows)} != POSITION count {nv}")
            nrm = cols.get("NORMAL")
            if nrm:
                bad = sum(1 for r in nrm if abs(math.sqrt(sum(x * x for x in r)) - 1.0) > 2e-3)
                if bad:
                    c.err(f"{where}: {bad} NORMAL vectors are not unit length")
            uv = cols.get("TEXCOORD_0")
            if uv:
                bad = sum(1 for r in uv if not (-UV_EPS <= r[0] <= 1 + UV_EPS and -UV_EPS <= r[1] <= 1 + UV_EPS))
                if bad:
                    c.err(f"{where}: {bad} TEXCOORD_0 values outside [0, 1]")
            ii = p.get("indices")
            if ii is None:
                c.err(f"{where}: indices missing (non-indexed geometry is not in the contract)")
                continue
            accs = c.lst("accessors")
            if isinstance(ii, int) and 0 <= ii < len(accs):
                ia = accs[ii]
                if ia.get("type") != "SCALAR" or ia.get("componentType") not in (USHORT, UINT):
                    c.err(f"{where}: indices must be SCALAR UNSIGNED_SHORT/UNSIGNED_INT")
                elif nv:
                    want = UINT if nv >= 65535 else USHORT
                    if ia["componentType"] != want:
                        c.err(f"{where}: {nv} vertices need componentType {want} for indices, found {ia['componentType']}")
                if ia.get("count", 0) % 3:
                    c.err(f"{where}: index count {ia.get('count')} is not a multiple of 3")
            idx = c.read_accessor(ii, f"{where}.indices")
            if idx is not None and nv:
                flat = [r[0] for r in idx]
                if max(flat) >= nv:
                    c.err(f"{where}: index {max(flat)} >= vertex count {nv}")
                if accs[ii].get("componentType") == USHORT and 65535 in flat:
                    c.err(f"{where}: index 65535 is the primitive-restart value for UNSIGNED_SHORT")


def _materials(c: _Ctx) -> None:
    texs = c.lst("textures")
    for i, m in enumerate(c.lst("materials")):
        pbr = m.get("pbrMetallicRoughness", {})
        bct = pbr.get("baseColorTexture")
        if bct is not None and not (isinstance(bct.get("index"), int) and 0 <= bct["index"] < len(texs)):
            c.err(f"materials[{i}] baseColorTexture index {bct.get('index')!r} out of range")
        f = pbr.get("baseColorFactor")
        if f is not None and not (len(f) == 4 and all(isinstance(x, (int, float)) and 0.0 <= x <= 1.0 for x in f)):
            c.err(f"materials[{i}] baseColorFactor must be 4 floats in [0, 1], got {f!r}")
        for k in ("metallicFactor", "roughnessFactor"):
            if k in pbr and not 0.0 <= pbr[k] <= 1.0:
                c.err(f"materials[{i}] {k} {pbr[k]!r} outside [0, 1]")


# ---------------------------------------------------------------------------------------------------------------------


def _contract(c: _Ctx) -> None:
    d = c.doc
    ex = d.get("asset", {}).get("extras")
    if not isinstance(ex, dict) or ex.get("schema") != ASSET_SCHEMA:
        c.err(f"asset.extras.schema must be {ASSET_SCHEMA!r}")
    else:
        for k in ("segment_slug", "model_version", "model_hash", "footpath_side", "units", "up_axis", "travel", "right",
                  "length_s", "frame", "manifest", "gameplay_sha256"):
            if k not in ex:
                c.err(f"asset.extras missing {k!r}")
        if ex.get("units") != "metres" or ex.get("up_axis") != "+Y":
            c.err("asset.extras must declare units 'metres' and up_axis '+Y'")
    scenes, nodes, meshes, mats = c.lst("scenes"), c.lst("nodes"), c.lst("meshes"), c.lst("materials")
    if len(scenes) != 1 or scenes[0].get("name") != SCENE_NAME or scenes[0].get("nodes") != [0]:
        c.err(f"expected one scene named {SCENE_NAME!r} with nodes [0]")
    if not nodes or nodes[0].get("name") != ROOT_NODE:
        c.err(f"nodes[0] must be the root node {ROOT_NODE!r}")
        return
    children = nodes[0].get("children", [])
    for ni, n in enumerate(nodes):
        for k in ("matrix", "translation", "rotation", "scale"):
            if k in n:
                c.err(f"node {n.get('name')!r} has a {k}: all transforms must be identity (geometry in world coordinates)")
        if ni != 0 and ni not in children:
            c.err(f"node {n.get('name')!r} is not a child of the root")
    if "mesh" in nodes[0]:
        c.err("root node must not carry a mesh")
    names: list[str] = []
    mat_names = [m.get("name") for m in mats]
    if len(set(mat_names)) != len(mat_names) or not set(mat_names) <= set(MATERIAL_NAMES):
        c.err(f"materials must be a subset of {MATERIAL_NAMES}, found {mat_names}")
    for ci in children:
        if not isinstance(ci, int) or not 0 <= ci < len(nodes):
            continue
        n = nodes[ci]
        name = n.get("name")
        names.append(name)
        if name not in NODE_NAMES:
            c.err(f"node name {name!r} is not in the contract set")
            continue
        if n.get("children"):
            c.err(f"node {name}: leaf nodes must not have children")
        mi = n.get("mesh")
        if mi is None:
            c.err(f"node {name}: no mesh (nodes without geometry must be omitted)")
            continue
        prims = meshes[mi].get("primitives", [])
        if len(prims) != 1:
            c.err(f"node {name}: expected exactly one primitive, found {len(prims)}")
            continue
        p = prims[0]
        attrs = p.get("attributes", {})
        mname = mats[p["material"]].get("name") if isinstance(p.get("material"), int) and p["material"] < len(mats) else None
        if mname != NODE_MATERIAL[name]:
            c.err(f"node {name}: material {mname!r} but the contract says {NODE_MATERIAL[name]!r}")
        if ("TEXCOORD_0" in attrs) != NODE_HAS_UV[name]:
            c.err(f"node {name}: TEXCOORD_0 {'present' if 'TEXCOORD_0' in attrs else 'absent'}, contract says "
                  f"{'required' if NODE_HAS_UV[name] else 'forbidden'}")
        if "NORMAL" not in attrs:
            c.err(f"node {name}: NORMAL missing")
        needs_colour = NODE_MATERIAL[name] == "mat_flat" or name in GROUND_NODES and name != "Kerb"
        if needs_colour and "COLOR_0" not in attrs:
            c.err(f"node {name}: COLOR_0 missing (vertex colours carry the tint)")
        _node_extras(c, name, n.get("extras"), p)
    if len(set(names)) != len(names):
        c.err("duplicate node names")
    for must in ("Road",):
        if must not in names:
            c.err(f"required node {must!r} missing")
    c.rep.stats["nodes"] = names


def _tri_count(c: _Ctx, prim: dict[str, Any]) -> int:
    accs = c.lst("accessors")
    ii = prim.get("indices")
    return accs[ii]["count"] // 3 if isinstance(ii, int) and ii < len(accs) else 0


def _node_extras(c: _Ctx, name: str, ex: Any, prim: dict[str, Any]) -> None:
    if not isinstance(ex, dict):
        c.err(f"node {name}: extras missing")
        return
    icount = _tri_count(c, prim) * 3
    kind = ex.get("kind")
    if name in GROUND_NODES:
        if kind not in ("footpath", "road", "kerb", "ground"):
            c.err(f"node {name}: extras.kind {kind!r}")
        for k in ("side", "runs", "bins"):
            if k not in ex:
                c.err(f"node {name}: extras missing {k!r}")
        for b in ex.get("bins", []):
            if b.get("ground_source") not in ("satellite_hires", "satellite_overview", "interpolated", "procedural"):
                c.err(f"node {name}: bin {b.get('index')} ground_source {b.get('ground_source')!r}")
            if b.get("fidelity_level") not in (0, 1, 2, 3):
                c.err(f"node {name}: bin {b.get('index')} fidelity_level {b.get('fidelity_level')!r}")
        for r in ex.get("runs", []):
            if not r.get("s_from", 0) <= r.get("s_to", 0):
                c.err(f"node {name}: run with s_from > s_to {r}")
    elif name in FRONTAGE_NODES:
        if kind != "frontage" or "segments" not in ex:
            c.err(f"node {name}: extras must be {{kind: frontage, segments: [...]}}")
            return
        prev = None
        for sg in ex["segments"]:
            if sg.get("type") not in ("building", "wall", "gate"):
                c.err(f"node {name}: segment type {sg.get('type')!r}")
            if sg.get("source") not in ("osm", "vision", "entity", "interpolated", "prior"):
                c.err(f"node {name}: segment source {sg.get('source')!r}")
            if prev is not None and abs(sg["s_from"] - prev) > 1e-6:
                c.err(f"node {name}: frontage not contiguous at s={prev} -> {sg['s_from']}")
            prev = sg["s_to"]
            st, cnt = sg.get("index_start", -1), sg.get("index_count", -1)
            if st < 0 or cnt < 0 or st + cnt > icount or st % 3 or cnt % 3:
                c.err(f"node {name}: segment index range [{st}, +{cnt}) invalid for {icount} indices")
    elif name == "Frontage_Caps":
        if kind != "frontage_caps":
            c.err("node Frontage_Caps: extras.kind must be 'frontage_caps'")
    elif name in FURNITURE_NODES:
        if kind != "furniture" or ex.get("category") != FURNITURE_CATEGORY[name]:
            c.err(f"node {name}: extras must be kind furniture / category {FURNITURE_CATEGORY[name]!r}")
        ids = set()
        for e in ex.get("entities", []):
            for k in ("id", "type", "s", "t", "confidence", "fidelity_level", "index_start", "index_count"):
                if k not in e:
                    c.err(f"node {name}: entity missing {k!r}: {e.get('id')}")
            if e.get("id") in ids:
                c.err(f"node {name}: duplicate entity id {e.get('id')}")
            ids.add(e.get("id"))
            st, cnt = e.get("index_start", -1), e.get("index_count", -1)
            if st < 0 or cnt < 0 or st + cnt > icount or st % 3 or cnt % 3:
                c.err(f"node {name}: entity {e.get('id')} index range [{st}, +{cnt}) invalid for {icount} indices")


def _budgets(c: _Ctx, nbytes: int) -> None:
    nodes = [n for n in c.lst("nodes") if "mesh" in n]
    tris = sum(_tri_count(c, p) for m in c.lst("meshes") for p in m.get("primitives", []))
    length = (c.doc.get("asset", {}).get("extras") or {}).get("length_s", 0) or 0
    long_street = length > 150
    limits = {
        "primitives": (len(nodes), MAX_PRIMITIVES),
        "triangles": (tris, MAX_TRIANGLES * (2 if long_street else 1)),
        "materials": (len(c.lst("materials")), MAX_MATERIALS),
        "textures": (len(c.lst("textures")), MAX_TEXTURES),
        "bytes": (nbytes, MAX_GLB_BYTES_200M if long_street else MAX_GLB_BYTES),
    }
    c.rep.stats.update({k: v[0] for k, v in limits.items()})
    for k, (val, lim) in limits.items():
        if val > lim:
            c.err(f"budget: {k} {val} > {lim}")


# ---------------------------------------------------------------------------------------------------------------------
# Khronos reference validator (npm "gltf-validator" is a library without a CLI, so a tiny node driver is run through npx)

_NODE_DRIVER = r"""
const fs = require('fs'), path = require('path');
let dir = (process.env.PATH || '').split(path.delimiter).map(p => path.dirname(p))
  .find(d => fs.existsSync(path.join(d, 'gltf-validator', 'package.json')));
if (!dir) { console.error('gltf-validator module not found on PATH-derived node_modules'); process.exit(3); }
const v = require(path.join(dir, 'gltf-validator'));
const file = process.argv[2];
v.validateBytes(new Uint8Array(fs.readFileSync(file)), {uri: path.basename(file), maxIssues: 0})
  .then(r => { process.stdout.write(JSON.stringify(r)); })
  .catch(e => { console.error(String(e)); process.exit(4); });
"""


@dataclass
class KhronosResult:
    ran: bool
    reason: str = ""
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ran": self.ran, "reason": self.reason, "errors": self.errors, "warnings": self.warnings,
                "infos": self.infos, "version": self.version, "messages": self.messages}


def khronos_validate(path: str | Path, *, timeout_s: int = 240) -> KhronosResult:
    """Run the Khronos glTF-Validator on ``path`` via ``npx``. ``ran=False`` (+ reason) when node/npx/network is missing."""
    npx, node = shutil.which("npx"), shutil.which("node")
    if not npx or not node:
        return KhronosResult(False, "npx/node not found on PATH")
    with tempfile.TemporaryDirectory() as td:
        drv = Path(td) / "run_validator.js"
        drv.write_text(_NODE_DRIVER)
        try:
            cp = subprocess.run([npx, "--yes", "-p", KHRONOS_PACKAGE, "--", "node", str(drv), str(Path(path).resolve())],
                                capture_output=True, text=True, timeout=timeout_s, cwd=td)
        except subprocess.TimeoutExpired:
            return KhronosResult(False, f"npx timed out after {timeout_s}s (network?)")
    if cp.returncode != 0 or not cp.stdout.strip():
        return KhronosResult(False, f"npx {KHRONOS_PACKAGE} failed (exit {cp.returncode}): {(cp.stderr or '').strip()[-300:]}")
    try:
        rep = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return KhronosResult(False, f"validator output is not JSON: {cp.stdout[:200]!r}")
    iss = rep.get("issues", {})
    return KhronosResult(True, "", int(iss.get("numErrors", 0)), int(iss.get("numWarnings", 0)), int(iss.get("numInfos", 0)),
                         [m for m in iss.get("messages", []) if m.get("severity", 9) <= 1],
                         str(rep.get("validatorVersion", "")))
