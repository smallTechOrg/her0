"""``StreetMesh`` - the engine-neutral, in-memory mesh the glb writer serialises (spec/portability.md section 3).

Produced by ``hero.export.mesh_builder.build_street_mesh`` (S6), serialised by ``hero.export.glb.write_glb`` (S7).
Plain dataclasses with plain Python floats/ints (no numpy). Coordinates are the **street frame** (metres, +Y up,
travel toward -Z at s=0, +X right), all node transforms identity. ``nodes`` order is the order of the glb ``scene.nodes``.

Contract constants (node names, materials, budgets) live here so every slice shares one definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCENE_NAME = "Street"
ROOT_NODE = "Street"
ASSET_SCHEMA = "hero-street-glb/1"

NODE_NAMES = (
    "Footpath", "Footpath_Opposite", "Road", "Kerb", "Ground_L", "Ground_R",
    "Frontage_L", "Frontage_R", "Frontage_Caps",
    "Furniture_Poles", "Furniture_Trees", "Furniture_Boxes", "Furniture_Railings", "Furniture_Decals",
)
GROUND_NODES = ("Footpath", "Footpath_Opposite", "Road", "Ground_L", "Ground_R", "Kerb")
FRONTAGE_NODES = ("Frontage_L", "Frontage_R")
FURNITURE_NODES = ("Furniture_Poles", "Furniture_Trees", "Furniture_Boxes", "Furniture_Railings", "Furniture_Decals")
FURNITURE_CATEGORY = {
    "Furniture_Poles": "poles", "Furniture_Trees": "trees", "Furniture_Boxes": "boxes",
    "Furniture_Railings": "railings", "Furniture_Decals": "decals",
}
NODE_MATERIAL = {
    "Footpath": "mat_ground", "Footpath_Opposite": "mat_ground", "Road": "mat_ground",
    "Kerb": "mat_kerb", "Ground_L": "mat_ground", "Ground_R": "mat_ground",
    "Frontage_L": "mat_frontage", "Frontage_R": "mat_frontage", "Frontage_Caps": "mat_flat",
    **{n: "mat_flat" for n in FURNITURE_NODES},
}
NODE_HAS_UV = {n: m in ("mat_ground", "mat_frontage") and n != "Kerb" for n, m in NODE_MATERIAL.items()}
MATERIAL_NAMES = ("mat_ground", "mat_kerb", "mat_frontage", "mat_flat")
KERB_COLOUR = "#b9b9b9"
GROUND_BAND_T = 14.0  # ground atlas covers t in [-14, +14]
FACADE_MAX_H = 12.0

# Budgets (100 m fixtures; x2 for 200 m streets except primitives)
MAX_PRIMITIVES = 14
MAX_TRIANGLES = 12_000
MAX_MATERIALS = 4
MAX_TEXTURES = 2
MAX_GLB_BYTES = 1_500_000
MAX_GLB_BYTES_200M = 2_500_000


@dataclass
class MeshMaterial:
    name: str
    base_color_hex: str = "#ffffff"  # baseColorFactor (white for textured / vertex-coloured materials)
    texture: str | None = None  # key into StreetMesh.textures
    metallic: float = 0.0
    roughness: float = 1.0


@dataclass
class MeshNode:
    """One glTF node = one mesh = one primitive. Arrays are flat-by-vertex lists."""

    name: str
    material: str
    positions: list[tuple[float, float, float]]  # millimetre-rounded
    normals: list[tuple[float, float, float]]
    indices: list[int]  # triangle list, CCW seen from the front face
    uvs: list[tuple[float, float]] | None = None  # TEXCOORD_0 (ground/frontage only)
    colors: list[tuple[int, int, int, int]] | None = None  # COLOR_0 UNSIGNED_BYTE normalised RGBA
    extras: dict[str, Any] = field(default_factory=dict)  # node.extras, key order = spec section 3.1

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3

    @property
    def vertex_count(self) -> int:
        return len(self.positions)


@dataclass
class StreetMesh:
    asset_extras: dict[str, Any]  # glTF asset.extras (spec section 3.1)
    nodes: list[MeshNode]
    materials: list[MeshMaterial]
    textures: dict[str, bytes]  # name -> JPEG bytes ("atlas_ground", "atlas_facade")
    length_s: float = 0.0

    def node(self, name: str) -> MeshNode | None:
        for n in self.nodes:
            if n.name == name:
                return n
        return None

    @property
    def triangles(self) -> int:
        return sum(n.triangle_count for n in self.nodes)

    @property
    def vertices(self) -> int:
        return sum(n.vertex_count for n in self.nodes)

    def stats(self) -> dict[str, Any]:
        """``manifest.mesh_stats`` (budget check without opening the glb)."""
        return {
            "primitives": len(self.nodes),
            "triangles": self.triangles,
            "vertices": self.vertices,
            "materials": len({n.material for n in self.nodes}),
            "textures": len(self.textures),
            "nodes": [{"name": n.name, "triangles": n.triangle_count, "material": n.material} for n in self.nodes],
        }
