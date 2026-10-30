"""Create the canonical Phase-2 demo street through the admin API and write the placement evidence image.

    uv run python -m hero.tools.create_demo_street [--base-url http://127.0.0.1:PORT] [--out docs/evidence/placement.png]

Flow (the same calls the admin UI makes): resolve "100 Feet Road, Indiranagar" -> the candidate within 400 m of
12.9711, 77.6413 that carries a road heading -> ``from-address`` (half length 50 m, west footpath) -> ``placement-preview``
-> the annotated image is saved to ``docs/evidence/placement.png`` for a human to confirm the line lies on the road.
Idempotent: an existing segment with the demo name is reused.

By default the API runs in-process (``fastapi.testclient``) against the configured database, so no dev server is
needed; ``--base-url`` talks to a running server instead. Costs: geocoding + one cached Static Maps tile; no Claude.
Exit codes: 0 ok, 1 error, 2 the reference line is not verified against OSM road geometry.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from hero.config import PROJECT_ROOT, get_settings
from hero.scene.geo import haversine_m

DEFAULT_OUT = PROJECT_ROOT / "docs" / "evidence" / "placement.png"
MAX_CANDIDATE_DISTANCE_M = 400.0
MAX_SNAP_M = 25.0
LENGTH_TOL_M = 3.0
EXPECTED_LENGTH_M = 100.0


class DemoStreetError(RuntimeError):
    pass


class _Response(Protocol):
    status_code: int
    content: bytes

    def json(self) -> Any: ...


class _Client(Protocol):
    def get(self, url: str, **kw: Any) -> _Response: ...
    def post(self, url: str, **kw: Any) -> _Response: ...


@dataclass
class DemoResult:
    slug: str
    created: bool
    image_path: Path
    placement: dict[str, Any]
    problems: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return bool(self.placement.get("placement_verified"))


def _data(resp: _Response, what: str) -> Any:
    body = resp.json()
    if resp.status_code >= 400 or not body.get("ok"):
        err = body.get("error") or {}
        raise DemoStreetError(f"{what} failed ({resp.status_code}): {err.get('message', body)}")
    return body["data"]


def pick_candidate(candidates: list[dict[str, Any]], hint: dict[str, float],
                   max_distance_m: float = MAX_CANDIDATE_DISTANCE_M) -> dict[str, Any]:
    """The nearest candidate within ``max_distance_m`` of ``hint`` that has a road heading."""
    scored = sorted(((haversine_m(hint["lat"], hint["lon"], c["lat"], c["lon"]), c) for c in candidates),
                    key=lambda x: x[0])
    for dist, cand in scored:
        if dist <= max_distance_m and cand.get("heading_deg") is not None:
            return cand
    seen = "; ".join(f"{c.get('label', '?')} ({d:.0f} m, heading {c.get('heading_deg')})" for d, c in scored) or "none"
    raise DemoStreetError(f"No geocoder candidate within {max_distance_m:.0f} m of {hint['lat']}, {hint['lon']} has a "
                          f"road heading. Candidates: {seen}")


def create_demo_street(client: _Client, *, out_path: Path = DEFAULT_OUT, say=print) -> DemoResult:
    status = _data(client.get("/api/admin/status"), "status")
    demo = status["demo_street"]
    existing = next((s for s in _data(client.get("/api/admin/segments"), "segment list") if s["name"] == demo["name"]),
                    None)
    created = existing is None
    if existing is None:
        found = _data(client.post("/api/admin/segments/resolve", json={"address": demo["query"], "city": demo["city"]}),
                      "resolve")
        cand = pick_candidate(found["candidates"], demo["hint"])
        say(f"resolved {cand['label']!r} at {cand['lat']:.5f}, {cand['lon']:.5f} (heading {cand['heading_deg']:.0f} deg, "
            f"{cand.get('heading_source')})")
        existing = _data(client.post("/api/admin/segments/from-address", json={
            "name": demo["name"], "city": demo["city"], "lat": cand["lat"], "lon": cand["lon"],
            "heading_deg": cand["heading_deg"], "half_length_m": demo["half_length_m"],
            "footpath_side": demo["footpath_side"]}), "from-address")
        say(f"created segment {existing['slug']!r} ({existing['length_m']} m)")
    else:
        say(f"segment {existing['slug']!r} already exists; reusing it")
    slug = existing["slug"]

    placement = _data(client.post(f"/api/admin/segments/{slug}/placement-preview"), "placement-preview")
    image = client.get(placement["url"])
    if image.status_code != 200 or not image.content:
        raise DemoStreetError(f"could not download the placement image {placement['url']} ({image.status_code})")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(image.content)

    ref = placement["reference_line"]
    problems: list[str] = []
    if not str(ref["source"]).startswith("osm"):
        problems.append(f"reference line source is {ref['source']!r}, not OSM road geometry")
    if ref.get("snap_m", 0.0) > MAX_SNAP_M:
        problems.append(f"the line snaps {ref['snap_m']} m from the geocoded centre (max {MAX_SNAP_M:.0f} m)")
    if abs(ref["length_s"] - EXPECTED_LENGTH_M) > LENGTH_TOL_M:
        problems.append(f"length {ref['length_s']} m is not {EXPECTED_LENGTH_M:.0f} +- {LENGTH_TOL_M:.0f} m")
    say(f"placement: source={ref['source']} way={ref.get('osm_way_id')} name={ref.get('name')} length={ref['length_s']} m "
        f"snap={ref.get('snap_m')} m verified={placement['placement_verified']}; image -> {out_path}")
    for w in placement.get("warnings", []):
        say(f"warning: {w}")
    for p in problems:
        say(f"PROBLEM: {p}")
    return DemoResult(slug=slug, created=created, image_path=out_path, placement=placement, problems=problems)


def _default_client() -> _Client:
    from fastapi.testclient import TestClient

    from hero.app import create_app

    client = TestClient(create_app())
    client.headers.update({"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()})
    return client


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create the canonical demo street and write the placement evidence image.")
    ap.add_argument("--base-url", default=None, help="talk to a running server instead of running the API in-process")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        if args.base_url:
            import httpx

            client: _Client = httpx.Client(base_url=args.base_url, timeout=60.0,
                                           headers={"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()})
        else:
            client = _default_client()
        result = create_demo_street(client, out_path=args.out)
    except DemoStreetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"demo street {result.slug!r}: open {result.image_path} and confirm the cyan line lies on the road.")
    return 0 if result.verified and not result.problems else 2


if __name__ == "__main__":
    raise SystemExit(main())
