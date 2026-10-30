"""Street export bundle (spec/portability.md): glb mesh, manifest, OpenDRIVE, gameplay-as-data.

Helpers for the source ``gameplay.json`` (the only place gameplay numbers live). Other modules (mesh builder,
glb writer, manifest, xodr, bundle, pipeline) are owned by later slices.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

GAMEPLAY_JSON = Path(__file__).with_name("gameplay.json")
SCHEMAS_DIR = Path(__file__).with_name("schemas")
GENERATOR = "hero.export/1"


def gameplay_json_path() -> Path:
    return GAMEPLAY_JSON


def gameplay_bytes() -> bytes:
    return GAMEPLAY_JSON.read_bytes()


def gameplay_sha256() -> str:
    return hashlib.sha256(gameplay_bytes()).hexdigest()


def load_gameplay() -> dict:
    return json.loads(gameplay_bytes())
