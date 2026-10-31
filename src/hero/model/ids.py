"""Deterministic identifiers (spec/data.md -> "Evidence, confidence and state rules" -> Stable ids).

Ids are minted once and never change; the same inputs always give the same id. Pure.

Float formatting: coordinates are rounded to one decimal and formatted with ``:.1f`` after
normalising ``-0.0`` to ``0.0``, so the digest never depends on a sign-of-zero quirk.
"""

from __future__ import annotations

import hashlib

from hero.imagery.cache import ImageryCache


def _f1(x: float) -> str:
    return f"{round(x, 1) + 0.0:.1f}"


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def entity_id(segment_slug: str, type: str, first_observation_id: str, s: float, t: float, ordinal: int = 0) -> str:
    """``"ent-" + sha1(f"{slug}|{type}|{first_obs}|{round(s,1)}|{round(t,1)}|{ordinal}")[:10]``.

    ``ordinal`` counts same-type entities already minted from the same first observation.
    ``type`` may be an ``EntityType`` (a ``StrEnum``) or its string value.
    """
    return "ent-" + _sha1(f"{segment_slug}|{str(type)}|{first_observation_id}|{_f1(s)}|{_f1(t)}|{ordinal}")[:10]


def building_id(*, osm_way_id: int | None = None, segment_slug: str | None = None, side: str | None = None,
                s_center: float | None = None) -> str:
    """``bld-osm-<way_id>`` for OSM buildings, else ``bld-v-<sha1(slug|side|round(s_center))[:8]>``."""
    if osm_way_id is not None:
        return f"bld-osm-{int(osm_way_id)}"
    if segment_slug is None or side is None or s_center is None:
        raise ValueError("a vision building id needs segment_slug, side and s_center")
    return "bld-v-" + _sha1(f"{segment_slug}|{side}|{round(s_center)}")[:8]


def contradiction_id(kind: str, *parts: object) -> str:
    """``ctr-<sha1(kind|part|part...)[:8]>`` (not in the spec table; merge needs stable contradiction ids)."""
    return "ctr-" + _sha1("|".join([str(kind), *(str(p) for p in parts)]))[:8]


def obs_key_google(kind: str, canonical_params: dict) -> str:
    """Google observation key = ``ImageryCache.key`` (the cache file stem, 64 hex chars)."""
    return ImageryCache.key(kind, canonical_params)


def obs_key_upload(sha256: str) -> str:
    """Uploaded-photo key ``upload:<sha256>`` (<= 128 chars)."""
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower()):
        raise ValueError("sha256 must be 64 hex characters")
    return f"upload:{sha256.lower()}"
