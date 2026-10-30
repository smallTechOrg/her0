"""An ``httpx.MockTransport`` standing in for Google Static Maps / Street View (+ Nominatim / Overpass).

Serves the fixtures in this directory. Panoramas sit every ``pano_spacing_m`` along a reference line, ``lateral_m``
to its left, so neighbouring stations resolve to the same pano (the dedupe path). Every request is logged in
``calls`` so tests can assert how many PAID requests were made.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from PIL import Image, ImageDraw

from hero.model.refline import ReferenceLine

HERE = Path(__file__).parent


def fixture_json(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def fixture_bytes(name: str) -> bytes:
    return (HERE / name).read_bytes()


def straight_ref(length_m: float = 100.0, lat: float = 12.9740, lon: float = 77.6405) -> ReferenceLine:
    """A north-bound straight line of ``length_m`` metres (built from two points)."""
    from hero.scene.geo import METERS_PER_DEG_LAT
    return ReferenceLine.from_latlons([(lat, lon), (lat + length_m / METERS_PER_DEG_LAT, lon)])


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


class FakeGoogle:
    def __init__(self, ref: ReferenceLine, *, pano_spacing_m: float = 15.0, lateral_m: float = 3.0,
                 coverage: tuple[float, float] | None = None, placeholder_zooms: frozenset[int] = frozenset(),
                 frame_status: int = 200, metadata_status_code: int = 200, require_key: str | None = "test-key"):
        self.ref = ref
        self.pano_spacing_m = pano_spacing_m
        self.lateral_m = lateral_m
        self.coverage = coverage  # (s_from, s_to) with panoramas; None = whole line
        self.placeholder_zooms = placeholder_zooms
        self.frame_status = frame_status
        self.metadata_status_code = metadata_status_code
        self.require_key = require_key
        self.calls: list[dict] = []
        self._sv_base = Image.open(HERE / "streetview_frame.jpg").convert("RGB")
        self._tile = Image.open(HERE / "satellite_tile.png").convert("RGB")

    # ---------------------------------------------------------------- helpers

    @property
    def paid_calls(self) -> list[dict]:
        return [c for c in self.calls if c["kind"] in ("satellite", "streetview")]

    def calls_of(self, kind: str) -> list[dict]:
        return [c for c in self.calls if c["kind"] == kind]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def _panos(self) -> list[tuple[str, float, float, float]]:
        out = []
        lo, hi = self.coverage or (-1e9, 1e9)
        k = 0
        while k * self.pano_spacing_m <= self.ref.length_s + 1e-6:
            s = k * self.pano_spacing_m
            if lo <= s <= hi:
                lat, lon = self.ref.point_at(s, self.lateral_m)
                out.append((f"PANO_{k:03d}", lat, lon, s))
            k += 1
        return out

    # ---------------------------------------------------------------- transport

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        path = request.url.path
        host = request.url.host
        if host == "maps.googleapis.com":
            if self.require_key is not None and q.get("key") != self.require_key:
                self.calls.append({"kind": "denied", "path": path})
                return httpx.Response(403, json=fixture_json("error_403.json"))
            if path.endswith("/streetview/metadata"):
                return self._metadata(q)
            if path.endswith("/streetview"):
                return self._frame(q)
            if path.endswith("/staticmap"):
                return self._static(q)
        if host == "nominatim.openstreetmap.org":
            self.calls.append({"kind": "nominatim"})
            return httpx.Response(200, json=fixture_json("nominatim_reverse_road.json"))
        if "overpass" in host:
            self.calls.append({"kind": "overpass"})
            return httpx.Response(200, json=fixture_json("overpass_roads.json"))
        return httpx.Response(404, text=f"unmocked {request.url}")

    def _metadata(self, q: dict) -> httpx.Response:
        self.calls.append({"kind": "streetview_metadata", "location": q["location"]})
        if self.metadata_status_code != 200:
            return httpx.Response(self.metadata_status_code, json=fixture_json("error_403.json"))
        lat, lon = (float(x) for x in q["location"].split(","))
        best, best_d = None, 1e9
        for pano in self._panos():
            d = ((pano[1] - lat) * 111_320.0) ** 2 + ((pano[2] - lon) * 111_320.0 * 0.975) ** 2
            if d < best_d:
                best, best_d = pano, d
        if best is None or best_d ** 0.5 > float(q.get("radius", 50)):
            return httpx.Response(200, json=fixture_json("metadata_zero_results.json"))
        body = fixture_json("metadata_ok.json")
        body.update(pano_id=best[0], location={"lat": best[1], "lng": best[2]})
        return httpx.Response(200, json=body)

    def _frame(self, q: dict) -> httpx.Response:
        self.calls.append({"kind": "streetview", "pano": q.get("pano"), "heading": q.get("heading")})
        if self.frame_status != 200:
            return httpx.Response(self.frame_status, json=fixture_json("error_403.json"))
        img = self._sv_base.copy()
        ImageDraw.Draw(img).text((20, 20), f"{q.get('pano')} {q.get('heading')}", fill=(255, 255, 255))
        return httpx.Response(200, content=_jpeg(img), headers={"content-type": "image/jpeg"})

    def _static(self, q: dict) -> httpx.Response:
        zoom = int(q["zoom"])
        self.calls.append({"kind": "satellite", "zoom": zoom, "center": q["center"]})
        if zoom in self.placeholder_zooms:
            return httpx.Response(200, content=fixture_bytes("placeholder_tile.png"),
                                  headers={"content-type": "image/png"})
        img = self._tile.copy()
        ImageDraw.Draw(img).text((10, 10), f"{q['center']} z{zoom}", fill=(255, 255, 255))
        return httpx.Response(200, content=_png(img), headers={"content-type": "image/png"})


def make_imagery(tmp_path: Path, fake: FakeGoogle, run_id: str | None, *, key: str = "test-key"):
    """A ``GoogleImagery`` over ``fake`` with its own cache dir under ``tmp_path`` (no real network, no real key)."""
    from pydantic import SecretStr

    from hero.config import get_settings
    from hero.imagery.cache import ImageryCache
    from hero.imagery.google import GoogleImagery

    settings = get_settings().model_copy(update={"google_maps_api_key": SecretStr(key), "data_dir": tmp_path})
    return settings, GoogleImagery(settings, ImageryCache(tmp_path / "imagery"), run_id, http=fake.client(),
                                   sleep=lambda _s: None)
