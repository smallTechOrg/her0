"""Scene contract: typed Scene JSON schema, geometry helpers and the walkability score."""

from hero.scene.schema import Scene, load_example_scene
from hero.scene.walkability import compute_walkability

__all__ = ["Scene", "compute_walkability", "load_example_scene"]
