"""``export.zip``: the deterministic engine-import bundle (spec/portability.md section 1 and 9)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from hero import export as _export_pkg
from hero.export.store import ExportRecord, sha256_hex

ZIP_NAMES = ("street.glb", "street.manifest.json", "street.xodr", "gameplay.json", "IMPORT.txt")
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_TAGS = "hero-street-glb/1, hero-street-manifest/1, hero-gameplay/1 (OpenDRIVE 1.7, best effort)"

IMPORT_TEMPLATE = """\
#hero street bundle - {slug} (model v{model_version}, build {sha8})
=====================================================================

Contains: street.glb, street.manifest.json, street.xodr, gameplay.json, IMPORT.txt
Schema tags: {tags}
mesh_sha256     {mesh_sha256}
manifest_sha256 {manifest_sha256}
xodr_sha256     {xodr_sha256}
gameplay_sha256 {gameplay_sha256}

Keep street.glb and street.manifest.json together. Treat the manifest as the data source; glb `extras` are a
convenience (Unity's glTFast does not surface them). Units are metres, +Y up, travel toward -Z at s=0, +X is the
right-hand side of travel. 1 glTF unit = 1 metre.
What a port must rebuild itself: all runtime gameplay code (see spec/portability.md sections 6 and 7 in the
source repository: runner state machine, deterministic obstacle spawner), camera, input, HUD, sky/fog/light,
avatar and obstacle visuals. Dynamic obstacles are NOT in the glb; draw them from gameplay.json
`obstacle_types[...].size_m`.

UNITY (2022 LTS or newer; any render pipeline)
 1. Window > Package Manager > + > Add package by name: com.unity.cloud.gltfast
 2. Drag street.glb into Assets/; glTFast produces a prefab; drag it into a scene (or load at runtime with
    GltfImport.Load).
 3. glTF is right-handed, Unity left-handed, so the importer mirrors one axis (glTFast's documented behaviour is to
    negate X; UnityGLTF negates Z - check your version). Do not assume which: apply to every manifest position the
    same flip the importer applied to the vertices, and verify once with the end point - the world position
    reference_line.points[last] = (x, z) must coincide with the end of the imported Road mesh's bounds.
 4. 1 glTF unit = 1 metre = 1 Unity unit.
 5. Parse street.manifest.json (JsonUtility / Newtonsoft) for lanes, colliders, tokens, spawn hints, gameplay.
 6. Materials import as the pipeline's PBR metallic-roughness shader; mat_flat is vertex-coloured (enable vertex
    colours in the shader graph if the default shader ignores them).

UNREAL (5.3+)
 1. Edit > Plugins: enable Interchange (and the glTF Importer if offered by the engine version); restart.
 2. Content Browser > Import street.glb (Interchange: import as static meshes, "Combine static meshes" off to keep
    one mesh per node, import materials/textures on).
 3. Unreal is Z-up, left-handed, centimetres: the importer applies a x100 scale and an axis conversion from glTF;
    the exact mapping depends on importer and version, so apply the same conversion to manifest positions and
    verify once with the street length (length_s ~ the imported Road bounds along its long axis) and the end point
    reference_line.points[last]. If the scale is wrong, set Import Uniform Scale to 100.
 4. Load street.manifest.json with a JSON parser (Json Blueprint Utilities plugin or C++ FJsonSerializer).

GODOT (4.x)
 1. Copy street.glb and the manifest into res://street/; the editor imports .glb natively (Meshes > Generate LODs
    off is fine).
 2. glTF is right-handed Y-up metres, exactly Godot's convention, travel toward -Z: no conversion.
 3. Drag the imported scene into a level; node names are Footpath, Road, Frontage_L, ...; glTF extras appear as node
    metadata (get_meta("extras")).
 4. FileAccess.open("res://street/street.manifest.json", ...) + JSON.parse_string for the manifest; build
    StaticBody3D colliders from colliders[] and sample lanes.stations for lane following.

The engine steps above come from the engines' public documentation and are not machine-verified.

OpenDRIVE: street.xodr is best effort (one road, no junctions, no signals, flat z).

ATTRIBUTION (mandatory in any UI that shows the street)
{attribution}
{notice}
"""


def _gameplay_for(rec: ExportRecord, manifest: bytes) -> bytes:
    src = _export_pkg.gameplay_bytes()
    if sha256_hex(src) == rec.gameplay_sha256:
        return src
    embedded = json.loads(manifest)["gameplay"]  # built with a non-default gameplay: reproduce it from the manifest
    return json.dumps(embedded, indent=2).encode("utf-8")


def _import_txt(rec: ExportRecord, manifest: bytes, slug: str, model_version: int) -> bytes:
    att = json.loads(manifest).get("attribution", {})
    lines = att.get("lines") or []
    text = IMPORT_TEMPLATE.format(
        slug=slug, model_version=model_version, sha8=rec.sha8, tags=_TAGS, mesh_sha256=rec.mesh_sha256,
        manifest_sha256=rec.manifest_sha256, xodr_sha256=rec.xodr_sha256, gameplay_sha256=rec.gameplay_sha256,
        attribution="\n".join(f" - {line}" for line in lines), notice=att.get("notice", ""))
    return text.encode("utf-8")


def _slug_of(rec: ExportRecord) -> str:
    return Path(rec.files["glb"]).parent.name


def build_bundle(rec: ExportRecord, data_dir: Path, *, model_version: int) -> bytes:
    """Deterministic zip (fixed order, timestamps, permissions; same record + files => same bytes)."""
    data_dir = Path(data_dir)
    glb = (data_dir / rec.files["glb"]).read_bytes()
    manifest = (data_dir / rec.files["manifest"]).read_bytes()
    xodr = (data_dir / rec.files["xodr"]).read_bytes()
    payloads = {
        "street.glb": glb,
        "street.manifest.json": manifest,
        "street.xodr": xodr,
        "gameplay.json": _gameplay_for(rec, manifest),
        "IMPORT.txt": _import_txt(rec, manifest, _slug_of(rec), model_version),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in ZIP_NAMES:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            zf.writestr(info, payloads[name], compresslevel=9)
    return buf.getvalue()
