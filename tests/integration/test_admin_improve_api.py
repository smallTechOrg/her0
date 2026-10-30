"""improve-plan, fill-gaps (preview + execute) and regenerate-with-what-we-have on the real app + test DB.

Nothing leaves the machine: Google/Anthropic are never reachable (guard fixture), runs are captured at the registry.
"""

from __future__ import annotations

import socket
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hero.config import get_settings, reset_settings
from hero.db import model_repo, repo
from hero.db.model_repo import ModelVersionCreate, ObservationCreate
from hero.db.repo import SegmentCreate
from hero.model import improve
from hero.model.fidelity import compute_fidelity
from hero.model.types import ModelKind, ObservationKind, ObservationSource, ViewKind
from hero.scene.schema import FootpathSide, LatLon
from tests.fixtures.street_fixtures import make_fidelity_map, make_model, straight_reference_line

ORIGIN = (12.97400, 77.64050)
END = (12.97490, 77.64050)
SHA = "a" * 64


@pytest.fixture()
def app_client(test_db, tmp_path, monkeypatch):
    from hero.app import create_app

    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path))
    reset_settings()
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c
    reset_settings()


@pytest.fixture()
def auth(app_client):
    class Redacted(dict):
        __repr__ = __str__ = lambda self: "<admin auth (redacted)>"  # noqa: E731

    return Redacted({"X-Hero-Admin-Password": get_settings().admin_password.get_secret_value()})


@pytest.fixture()
def no_network(monkeypatch):
    """Any outbound connection or run start fails the test: these endpoints must not spend."""
    import httpx

    from hero.pipeline import registry

    calls: list[str] = []

    def boom(*a, **k):
        calls.append("network")
        raise AssertionError("network access attempted")

    real_connect = socket.socket.connect

    def guarded_connect(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "localhost", "::1", "testserver"):
            boom()
        return real_connect(self, address, *a, **k)

    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(registry, "start_run", lambda *a, **k: boom())
    return calls


def new_segment(name: str, *, forward_only: bool = True, with_model: bool = True):
    seg = repo.create_segment(SegmentCreate(name=name, city="Bengaluru", start=LatLon(lat=ORIGIN[0], lon=ORIGIN[1]),
                                            end=LatLon(lat=END[0], lon=END[1]), footpath_side=FootpathSide.left))
    if not with_model:
        return seg
    ref = straight_reference_line(100.0, origin=ORIGIN)
    ids = []
    obs = {}
    for i, s in enumerate(range(0, 101, 10)):
        lat, lon = ref.point_at(float(s), 0.0)
        o, _ = model_repo.upsert_observation(ObservationCreate(
            segment_id=seg.id, obs_key=f"sv-{i}", kind=ObservationKind.streetview, source=ObservationSource.google_streetview,
            sha256=SHA, file_path=f"imagery/sv{i}.jpg", media_type="image/jpeg", width_px=640, height_px=640, bytes=10,
            lat=lat, lon=lon, heading_deg=ref.bearing_deg(float(s)), station_s=float(s), offset_t=0.0, view=ViewKind.forward))
        ids.append(o.id)
        obs[o.id] = o
    model_repo.save_version_only(seg.id, ModelVersionCreate(
        version=0, kind="prior", model=make_model(ref, slug=seg.slug), fidelity=make_fidelity_map(ref.length_s)))
    fused = make_model(ref, slug=seg.slug, version=1, kind=ModelKind.fused, fused_observation_ids=sorted(ids))
    model_repo.save_version_only(seg.id, ModelVersionCreate(
        version=1, kind="fused", parent_version=0, model=fused, fidelity=compute_fidelity(fused, obs)))
    return seg


class _Run:
    def __init__(self, rid="run-9"):
        self.id, self.status = rid, SimpleNamespace(value="queued")


# ---------------------------------------------------------------- auth / 404


@pytest.mark.parametrize("method,path", [("get", "improve-plan"), ("post", "fill-gaps/preview"), ("post", "fill-gaps"),
                                         ("post", "regenerate")])
def test_every_endpoint_needs_the_admin_password(app_client, method, path):
    seg = new_segment("Improve auth")
    resp = getattr(app_client, method)(f"/api/admin/segments/{seg.slug}/{path}")
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("method,path", [("get", "improve-plan"), ("post", "fill-gaps/preview"), ("post", "fill-gaps"),
                                         ("post", "regenerate")])
def test_unknown_segment_is_404(app_client, auth, method, path):
    resp = getattr(app_client, method)(f"/api/admin/segments/nope-nope/{path}", headers=auth)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------- improve-plan


def test_improve_plan_reports_fidelity_weakest_stretches_hints_and_actions(app_client, auth, no_network):
    seg = new_segment("Improve plan")
    resp = app_client.get(f"/api/admin/segments/{seg.slug}/improve-plan", headers=auth)
    assert resp.status_code == 200, resp.text
    d = resp.json()["data"]
    assert d["head_version"] == 1
    assert d["fidelity"]["overall"]["level"] == 1 and d["fidelity"]["overall"]["histogram_m"]["L1"] == pytest.approx(100.0)
    assert d["intervals"][0]["s_from"] == 0.0 and d["intervals"][-1]["s_to"] == 100.0
    iv = d["weakest"][0]
    assert iv["level"] == 1 and "left" in iv["weakest_reason"] and "L2" in iv["hint"]
    assert iv["actions"] == ["fill_gaps", "upload_photo"]
    assert len(d["weakest"]) <= 5
    a = d["actions"]
    assert a["fill_gaps"]["available"] and a["fill_gaps"]["default_body"] == {"target_level": 2, "max_images": 40, "max_usd": 1.0}
    assert a["add_imagery"]["defaults"]["sat_crop_zoom"] == 20
    assert a["regenerate_cached"]["cost_usd"] == 0.0 and a["regenerate_cached"]["unfused_observations"] == 0
    assert a["upload"]["s_range"] == [0.0, pytest.approx(100.0, abs=0.5)]
    assert {"file", "s", "side", "facing", "heading_deg", "captured_at"} <= set(a["upload"]["fields"])
    assert a["upload"]["accepts"] == ["image/jpeg", "image/png"] and a["upload"]["max_mb"] == get_settings().upload_max_mb
    assert len(d["steps"]) == 5 and d["steps"][0].startswith("1. Look at the coverage bar")
    assert set(d["gap_summary"]["uncovered"]) == {"ground", "frontage_modelled", "frontage_opposite", "footpath", "furniture"}
    assert no_network == []


def test_improve_plan_counts_unfused_observations_and_missing_extractions(app_client, auth):
    seg = new_segment("Improve unfused")
    model_repo.upsert_observation(ObservationCreate(
        segment_id=seg.id, obs_key="late", kind=ObservationKind.streetview, source=ObservationSource.google_streetview,
        sha256=SHA, file_path="imagery/late.jpg", media_type="image/jpeg", width_px=64, height_px=64, bytes=1,
        lat=ORIGIN[0], lon=ORIGIN[1], heading_deg=0.0, station_s=1.0, offset_t=0.0, view=ViewKind.forward))
    a = app_client.get(f"/api/admin/segments/{seg.slug}/improve-plan", headers=auth).json()["data"]["actions"]
    assert a["regenerate_cached"]["unfused_observations"] == 1 and a["regenerate_cached"]["missing_extractions"] == 1
    assert a["regenerate_cached"]["scene_stale"] is True   # no compiled v3 scene stored


def test_improve_plan_without_a_model_says_how_to_start(app_client, auth):
    seg = new_segment("Improve empty", with_model=False)
    d = app_client.get(f"/api/admin/segments/{seg.slug}/improve-plan", headers=auth).json()["data"]
    assert d["head_version"] is None and d["intervals"] == [] and d["weakest"] == []
    assert d["steps"][0].startswith("1. Run Check placement, then Generate once to create the first model")
    assert d["actions"]["fill_gaps"]["available"] is False


def test_improve_plan_survives_a_new_app_instance(app_client, auth):
    from hero.app import create_app

    seg = new_segment("Improve persist")
    first = app_client.get(f"/api/admin/segments/{seg.slug}/improve-plan", headers=auth).json()["data"]
    with TestClient(create_app()) as fresh:
        again = fresh.get(f"/api/admin/segments/{seg.slug}/improve-plan", headers=auth).json()["data"]
    assert first == again


# ---------------------------------------------------------------- fill-gaps preview


def test_preview_plans_targets_with_cost_estimate_and_no_spend(app_client, auth, no_network):
    seg = new_segment("Preview")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps/preview", headers=auth, json={})
    assert resp.status_code == 200, resp.text
    d = resp.json()["data"]
    assert d["counts"]["planned"] == len(d["images"]) > 0
    assert d["counts"]["new"] + d["counts"]["cached"] == d["counts"]["planned"]
    views = {i["view"] for i in d["images"] if i["kind"] == "streetview"}
    assert {"left", "right"} <= views
    assert any(i["kind"] == "satellite_crop" and i["zoom"] == 20 for i in d["images"])
    assert all({"kind", "view", "station_s", "lat", "lon", "heading_deg", "zoom", "cached", "reason"} <= set(i) for i in d["images"])
    assert d["plan"]["targets"] and d["intervals_targeted"][0]["from_level"] == 1
    est = d["est"]
    assert est["total_usd"] == pytest.approx(est["google_usd"] + est["claude_usd"], abs=1e-4) and est["model"]
    want = improve.estimate_cost(sum(1 for i in d["images"] if i["kind"] == "streetview"),
                                 sum(1 for i in d["images"] if i["kind"] == "satellite_crop"), est["model"])
    assert est["claude_usd"] == want.claude_usd and est["google_usd"] == want.google_usd
    assert d["trimmed"] is False and d["will_exceed_budget"] is False
    assert no_network == []


def test_preview_is_deterministic_and_prices_with_the_requested_model(app_client, auth):
    seg = new_segment("Preview model")
    url = f"/api/admin/segments/{seg.slug}/fill-gaps/preview"
    a = app_client.post(url, headers=auth, json={}).json()["data"]
    b = app_client.post(url, headers=auth, json={}).json()["data"]
    c = app_client.post(url, headers=auth, json={"model": get_settings().vision_model}).json()["data"]
    assert a == b and c["est"] == a["est"]
    haiku = app_client.post(url, headers=auth, json={"model": "claude-haiku-4-5"})
    if haiku.status_code == 200:   # priced rows only; the default list may refuse non-listed ids (then 422 below)
        assert haiku.json()["data"]["est"]["claude_usd"] < a["est"]["claude_usd"]
        assert haiku.json()["data"]["images"] == a["images"]
    else:
        assert haiku.status_code == 422


def test_preview_limits_trim_and_flag(app_client, auth):
    seg = new_segment("Preview trim")
    url = f"/api/admin/segments/{seg.slug}/fill-gaps/preview"
    full = app_client.post(url, headers=auth, json={}).json()["data"]
    few = app_client.post(url, headers=auth, json={"max_images": 3}).json()["data"]
    assert few["trimmed"] is True and len(few["images"]) <= 3 < len(full["images"])
    cheap = app_client.post(url, headers=auth, json={"max_usd": 0.05}).json()["data"]
    assert cheap["trimmed"] is True and cheap["est"]["total_usd"] <= 0.05 + 1e-9
    ranged = app_client.post(url, headers=auth, json={"s_range": {"s_from": 40, "s_to": 60}}).json()["data"]
    assert ranged["images"] and all(25 <= i["station_s"] <= 75 for i in ranged["images"])


def test_preview_flags_will_exceed_budget_only_against_the_remaining_global_budget(app_client, auth, monkeypatch):
    from hero.llm.budget import BudgetGuard

    seg = new_segment("Preview budget")
    monkeypatch.setattr(BudgetGuard, "remaining_usd", lambda self: 0.001)
    d = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps/preview", headers=auth, json={}).json()["data"]
    assert d["will_exceed_budget"] is True and d["images"]       # informational: the plan is still returned


def test_preview_nothing_to_do_says_so(app_client, auth):
    import json as _json
    from pathlib import Path

    from hero.model.types import FidelityMap

    seg = new_segment("Preview nothing", with_model=False)
    ref = straight_reference_line(100.0, origin=ORIGIN)
    rec = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "bellandur" / "fidelity_v1.json"
    fid = FidelityMap.model_validate(_json.loads(rec.read_text()))
    model_repo.save_version_only(seg.id, ModelVersionCreate(version=0, kind="prior", model=make_model(ref, slug=seg.slug),
                                                            fidelity=fid))
    d = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps/preview", headers=auth, json={}).json()["data"]
    assert d["images"] == [] and d["note"] == "Nothing to fill: every stretch is at the target level."
    assert d["est"]["total_usd"] == 0.0


@pytest.mark.parametrize("body", [{"target_level": 1}, {"target_level": 4}, {"max_images": 0}, {"max_images": 201},
                                  {"max_usd": 0.01}, {"max_usd": 6}, {"s_range": {"s_from": 10, "s_to": 5}},
                                  {"bogus": 1}, {"model": "gpt-nope"}, {"s_range": {"s_from": 500, "s_to": 600}}])
def test_preview_rejects_bad_input_with_422(app_client, auth, body):
    seg = new_segment("Preview bad")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps/preview", headers=auth, json=body)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error", resp.text


def test_preview_without_a_model_is_422(app_client, auth):
    seg = new_segment("Preview no model", with_model=False)
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps/preview", headers=auth, json={})
    assert resp.status_code == 422 and "Generate once" in resp.json()["error"]["message"]


# ---------------------------------------------------------------- fill-gaps execute


def test_fill_gaps_starts_a_generate_run_for_exactly_the_previewed_targets(app_client, auth, monkeypatch):
    from hero.pipeline import registry

    seen = {}

    def fake_start(slug, *, mode="generate", plan_overrides=None, model=None):
        seen.update(slug=slug, mode=mode, overrides=plan_overrides)
        return _Run()

    monkeypatch.setattr(registry, "start_run", fake_start)
    seg = new_segment("Fill run")
    prev = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps/preview", headers=auth, json={}).json()["data"]
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps", headers=auth, json={})
    assert resp.status_code == 202, resp.text
    d = resp.json()["data"]
    assert d["run_id"] == "run-9" and d["status"] == "queued" and d["mode"] == "generate"
    assert d["plan_summary"] == {"new": prev["counts"]["new"], "cached": prev["counts"]["cached"],
                                 "est_total_usd": prev["est"]["total_usd"]}
    assert seen["mode"] == "generate" and seen["slug"] == seg.slug and set(seen["overrides"]) == {"targets"}
    got = [(t["kind"], t["station_s"], t.get("view"), t.get("zoom")) for t in seen["overrides"]["targets"]]
    want = [(i["kind"], i["station_s"], i["view"], i["zoom"]) for i in prev["plan"]["targets"] and
            [{"kind": t["kind"], "station_s": t["station_s"], "view": t.get("view"), "zoom": t.get("zoom")}
             for t in prev["plan"]["targets"]]]
    assert got == want and len(got) == prev["counts"]["planned"]


def test_fill_gaps_without_fuse_is_acquire_only_and_unpriced_for_claude(app_client, auth, monkeypatch):
    from hero.pipeline import registry

    seen = {}
    monkeypatch.setattr(registry, "start_run", lambda slug, *, mode="generate", plan_overrides=None, model=None:
                        seen.update(mode=mode) or _Run())
    seg = new_segment("Fill acquire")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps", headers=auth, json={"fuse": False})
    assert resp.status_code == 202 and resp.json()["data"]["mode"] == "acquire" and seen["mode"] == "acquire"
    prev = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps/preview", headers=auth, json={"fuse": False}).json()["data"]
    assert prev["est"]["claude_usd"] == 0.0


def test_fill_gaps_passes_the_model_to_the_registry(app_client, auth, monkeypatch):
    from hero.pipeline import registry

    seen = {}

    def fake_start(slug, *, mode="generate", plan_overrides=None, model=None):
        seen["model"] = model
        return _Run()

    monkeypatch.setattr(registry, "start_run", fake_start)
    seg = new_segment("Fill model")
    model = get_settings().vision_model
    assert app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps", headers=auth, json={"model": model}).status_code == 202
    assert seen["model"] == model


@pytest.mark.parametrize("exc_name,status,code", [("RunInProgress", 409, "run_in_progress"),
                                                  ("BudgetExhausted", 402, "budget_exhausted"),
                                                  ("PlanError", 422, "validation_error")])
def test_fill_gaps_maps_registry_refusals(app_client, auth, monkeypatch, exc_name, status, code):
    from hero.pipeline import registry

    def refuse(*a, **k):
        if exc_name == "RunInProgress":
            raise registry.RunInProgress(SimpleNamespace(id="r1", status=SimpleNamespace(value="running")))
        if exc_name == "BudgetExhausted":
            raise registry.BudgetExhausted(5.0, 5.0)
        raise registry.PlanError("target station outside")

    monkeypatch.setattr(registry, "start_run", refuse)
    seg = new_segment(f"Fill {exc_name}")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps", headers=auth, json={})
    assert resp.status_code == status and resp.json()["error"]["code"] == code


def test_fill_gaps_with_nothing_to_fill_is_422_and_starts_nothing(app_client, auth, no_network):
    seg = new_segment("Fill nothing", with_model=False)
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps", headers=auth, json={})
    assert resp.status_code == 422 and no_network == []


def test_fill_gaps_targets_reach_the_real_registry_plan(app_client, auth, monkeypatch):
    """The targets we hand to start_run are accepted by the registry's own plan validation (no run is started)."""
    from hero.pipeline import registry

    seg = new_segment("Fill registry plan")
    captured = {}

    def check(slug, *, mode="generate", plan_overrides=None, model=None):
        segment = repo.require_segment(slug)
        items, info = registry._planned_items(segment, get_settings(), plan_overrides)
        captured["items"], captured["info"] = items, info
        return _Run()

    monkeypatch.setattr(registry, "start_run", check)
    prev = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps/preview", headers=auth, json={}).json()["data"]
    assert app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps", headers=auth, json={}).status_code == 202
    assert captured["info"].counts["streetview"] == sum(1 for i in prev["images"] if i["kind"] == "streetview")
    assert captured["info"].counts["satellite_crop"] == sum(1 for i in prev["images"] if i["kind"] == "satellite_crop")


# ---------------------------------------------------------------- regenerate with what we have


@pytest.fixture()
def fake_recompile(monkeypatch):
    """Guarded fake of ``hero.fusion.recompile.recompile_segment``: records calls, makes no network/LLM call."""
    calls = []

    def recompile_segment(slug, *, remerge=False, dry_run=False, data_dir=None):
        calls.append({"slug": slug, "remerge": remerge, "dry_run": dry_run})
        return SimpleNamespace(slug=slug, version=1, scene_schema_before=2, scene_schema_after=3, unchanged=False,
                               version_created=remerge, interpolated_m=25.0, dynamic_excluded=41, blank_bins=0,
                               missing_extractions=["obs-x"] if remerge else [], warnings=["w1"], cost_usd=0.0,
                               export_sha8="abcd1234", triangles=9000, primitives=12, mesh_sha256="f" * 64)

    mod = types.ModuleType("hero.fusion.recompile")
    mod.recompile_segment = recompile_segment
    monkeypatch.setitem(sys.modules, "hero.fusion.recompile", mod)
    return calls


def test_regenerate_compile_calls_the_recompile_tool_and_costs_nothing(app_client, auth, fake_recompile, no_network):
    seg = new_segment("Regen compile")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/regenerate", headers=auth, json={})
    assert resp.status_code == 200, resp.text
    d = resp.json()["data"]
    assert fake_recompile == [{"slug": seg.slug, "remerge": False, "dry_run": False}]
    assert d == {"mode": "compile", "version": 1, "version_created": False, "scene_schema": 3, "previous_scene_schema": 2,
                 "export": {"sha8": "abcd1234", "mesh_sha256": "f" * 64, "triangles": 9000, "primitives": 12},
                 "unchanged": False, "cost_usd": 0.0, "google_requests": 0, "anthropic_calls": 0, "interpolated_m": 25.0,
                 "dynamic_excluded": 41, "missing_extractions": [], "warnings": ["w1"]}
    assert no_network == []


def test_regenerate_remerge_lists_observations_that_need_generate(app_client, auth, fake_recompile):
    seg = new_segment("Regen remerge")
    d = app_client.post(f"/api/admin/segments/{seg.slug}/regenerate", headers=auth, json={"mode": "remerge"}).json()["data"]
    assert fake_recompile[0]["remerge"] is True
    assert d["mode"] == "remerge" and d["version_created"] is True and d["missing_extractions"] == ["obs-x"]
    assert d["cost_usd"] == 0.0 and d["google_requests"] == 0 and d["anthropic_calls"] == 0


def test_regenerate_never_touches_the_llm_or_google_clients(app_client, auth, fake_recompile, monkeypatch):
    import hero.imagery.google as google
    import hero.llm.anthropic_client as anth

    def boom(*a, **k):
        raise AssertionError("regenerate must not construct a paid client")

    monkeypatch.setattr(google.GoogleImagery, "__init__", boom)
    monkeypatch.setattr(anth, "AnthropicClient", boom, raising=False)
    seg = new_segment("Regen guard")
    assert app_client.post(f"/api/admin/segments/{seg.slug}/regenerate", headers=auth).status_code == 200


@pytest.mark.parametrize("body", [{"mode": "everything"}, {"mode": 3}, {"x": 1}])
def test_regenerate_rejects_bad_mode_with_422(app_client, auth, fake_recompile, body):
    seg = new_segment("Regen bad")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/regenerate", headers=auth, json=body)
    assert resp.status_code == 422 and fake_recompile == []


def test_regenerate_without_a_model_is_422(app_client, auth, fake_recompile):
    seg = new_segment("Regen none", with_model=False)
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/regenerate", headers=auth)
    assert resp.status_code == 422 and "Nothing to regenerate: generate once first." == resp.json()["error"]["message"]
    assert fake_recompile == []


def test_regenerate_while_a_run_is_active_is_409(app_client, auth, fake_recompile, monkeypatch):
    from hero.pipeline import registry

    monkeypatch.setattr(registry, "active_run", lambda: SimpleNamespace(id="r7", status=SimpleNamespace(value="running")))
    seg = new_segment("Regen busy")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/regenerate", headers=auth)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "run_in_progress" and fake_recompile == []


def test_fill_gaps_unknown_model_is_422_on_preview_and_start(app_client, auth, no_network):
    seg = new_segment("Fill bad model")
    for path in ("fill-gaps/preview", "fill-gaps"):
        resp = app_client.post(f"/api/admin/segments/{seg.slug}/{path}", headers=auth, json={"model": "gpt-not-a-model"})
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "validation_error", path
    assert no_network == []


def test_fill_gaps_registry_model_choice_error_is_422(app_client, auth, monkeypatch):
    from hero.llm.models import ModelChoiceError
    from hero.pipeline import registry

    def refuse(*a, **k):
        raise ModelChoiceError("model disabled", model="m", reason="disabled")

    monkeypatch.setattr(registry, "start_run", refuse)
    seg = new_segment("Fill model refused")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/fill-gaps", headers=auth, json={})
    assert resp.status_code == 422 and resp.json()["error"]["message"] == "model disabled"


def test_regenerate_remerge_unavailable_is_a_clear_422(app_client, auth, no_network):
    seg = new_segment("Regen remerge real")
    resp = app_client.post(f"/api/admin/segments/{seg.slug}/regenerate", headers=auth, json={"mode": "remerge"})
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "validation_error" and err["message"].startswith("remerge not available")
    assert no_network == []


def test_fill_gaps_without_fuse_still_validates_a_supplied_model(app_client, auth, monkeypatch):
    from hero.pipeline import registry

    started = []
    monkeypatch.setattr(registry, "start_run", lambda *a, **k: started.append(1) or _Run())
    seg = new_segment("Fill bad model")
    url = f"/api/admin/segments/{seg.slug}/fill-gaps"
    bad = app_client.post(url, headers=auth, json={"fuse": False, "model": "no-such-model"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"
    assert bad.json()["error"]["details"]["field"] == "model" if "details" in bad.json()["error"] else True
    assert started == [], "nothing started for an invalid model"
    ok = app_client.post(url, headers=auth, json={"fuse": False, "model": get_settings().vision_model})
    assert ok.status_code == 202 and started == [1]
