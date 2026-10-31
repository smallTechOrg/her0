"""Export a recorded real street (model, observations, a size-capped set of images) as test fixtures.

    uv run python -m hero.tools.export_recorded --db <COPY of hero.db> --data-dir <dir holding imagery/> \
        --slug bellandur-main-road-n-2 --out tests/fixtures/recorded/bellandur

Never opens the live database for writing: ``--db`` is opened read-only (``mode=ro``); take the copy first with
``--backup-from data/hero.db`` (``sqlite3`` online-backup API, safe while the server runs) which writes ``--db``.
Output layout (the ``load_fixture_street`` contract, minus the scene files)::

    manifest.json        slug, name, city, start, end, footpath_side, source version, image budget report
    model_v<N>.json      the head CanonicalModel
    fidelity_v<N>.json   its stored FidelityMap
    observations.json    list of Observation dicts the model references (file_path -> images/<basename>)
    images/<basename>    every image re-encoded as JPEG so that the total stays <= --max-bytes

Re-encoding changes the pixels, so each observation gets the new ``sha256``/``bytes``/``media_type`` and its note
records the original sha256. Pixel dimensions are unchanged (georef stays valid).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from PIL import Image

DEFAULT_MAX_BYTES = 4_000_000
QUALITIES = (82, 75, 68, 60, 52, 45, 38, 30)


def backup_copy(src: Path, dst: Path) -> None:
    """Online-backup ``src`` into a new file ``dst`` (read-only on ``src``)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    ro = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dst)
        try:
            ro.backup(out)
            out.execute("pragma journal_mode=delete")  # the copy is single-user: no -wal/-shm needed to read it
        finally:
            out.close()
    finally:
        ro.close()


def _jpeg(path: Path, quality: int) -> bytes:
    with Image.open(path) as im:
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def _encode_all(paths: dict[str, Path], max_bytes: int) -> tuple[int, dict[str, bytes]]:
    for q in QUALITIES:
        blobs = {k: _jpeg(p, q) for k, p in paths.items()}
        if sum(len(b) for b in blobs.values()) <= max_bytes:
            return q, blobs
    raise SystemExit(f"cannot fit {len(paths)} images in {max_bytes} bytes even at quality {QUALITIES[-1]}")


def export_recorded(db: Path, data_dir: Path, slug: str, out: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        seg = con.execute("select * from segments where slug=?", (slug,)).fetchone()
        if seg is None:
            raise SystemExit(f"no segment with slug {slug!r} in {db}")
        ver = con.execute("select * from model_versions where segment_id=? order by version desc limit 1",
                          (seg["id"],)).fetchone()
        if ver is None:
            raise SystemExit(f"segment {slug!r} has no model version")
        model = json.loads(ver["model_json"])
        fused = set(model.get("fused_observation_ids") or [])
        rows = [dict(r) for r in con.execute("select * from observations where segment_id=? order by obs_key, id",
                                              (seg["id"],))]
        fidelity = json.loads(ver["fidelity_json"])
    finally:
        con.close()
    rows = [r for r in rows if not fused or r["id"] in fused]
    paths: dict[str, Path] = {}
    for r in rows:
        p = data_dir / r["file_path"]
        if not p.is_file():
            raise SystemExit(f"image missing for observation {r['id']}: {p}")
        paths[r["id"]] = p
    quality, blobs = _encode_all(paths, max_bytes)

    (out / "images").mkdir(parents=True, exist_ok=True)
    for old in (out / "images").glob("*"):
        old.unlink()
    obs_out = []
    for r in rows:
        blob = blobs[r["id"]]
        sha = hashlib.sha256(blob).hexdigest()
        name = f"{sha}.jpg"
        (out / "images" / name).write_bytes(blob)
        note = (r["note"] + " " if r["note"] else "") + f"[recorded: re-encoded jpeg q{quality}; original sha256 {r['sha256'].strip()}]"
        obs_out.append({
            "id": r["id"], "segment_id": r["segment_id"], "obs_key": r["obs_key"], "kind": r["kind"],
            "source": r["source"], "sha256": sha, "file_path": f"imagery/{name}", "media_type": "image/jpeg",
            "width_px": r["width_px"], "height_px": r["height_px"], "bytes": len(blob), "lat": r["lat"],
            "lon": r["lon"], "heading_deg": r["heading_deg"], "pitch_deg": r["pitch_deg"], "fov_deg": r["fov_deg"],
            "zoom": r["zoom"], "scale": r["scale"], "meters_per_px": r["meters_per_px"], "pano_id": r["pano_id"],
            "capture_date": r["capture_date"], "station_s": r["station_s"], "offset_t": r["offset_t"],
            "view": r["view"], "georef": json.loads(r["georef_json"] or "{}"), "cost_usd": float(r["cost_usd"]),
            "acquired_run_id": r["acquired_run_id"], "note": note, "created_at": str(r["created_at"]).replace(" ", "T") + "Z",
        })
    v = ver["version"]
    dump = lambda o: json.dumps(o, indent=1, sort_keys=True, ensure_ascii=False) + "\n"  # noqa: E731
    (out / f"model_v{v}.json").write_text(dump(model), encoding="utf-8")
    (out / f"fidelity_v{v}.json").write_text(dump(fidelity), encoding="utf-8")
    (out / "observations.json").write_text(dump(obs_out), encoding="utf-8")
    manifest = {
        "slug": seg["slug"], "name": seg["name"], "city": seg["city"],
        "start": {"lat": seg["start_lat"], "lon": seg["start_lon"]}, "end": {"lat": seg["end_lat"], "lon": seg["end_lon"]},
        "footpath_side": seg["footpath_side"], "length_m": seg["length_m"], "heading_deg": seg["heading_deg"],
        "model_version": v, "model_hash": ver["model_hash"].strip(), "observations": len(obs_out),
        "image_bytes": sum(len(b) for b in blobs.values()), "image_jpeg_quality": quality, "image_budget_bytes": max_bytes,
    }
    (out / "manifest.json").write_text(dump(manifest), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, required=True, help="a COPY of hero.db (opened read-only)")
    ap.add_argument("--backup-from", type=Path, help="first write --db as an online-backup copy of this database")
    ap.add_argument("--data-dir", type=Path, required=True, help="directory containing imagery/ (file_path is relative to it)")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    a = ap.parse_args(argv)
    if a.backup_from:
        backup_copy(a.backup_from, a.db)
    m = export_recorded(a.db, a.data_dir, a.slug, a.out, max_bytes=a.max_bytes)
    print(json.dumps(m, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
