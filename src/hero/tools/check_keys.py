"""Scaffold-gate key validation. Prints presence booleans, model + ok, bytes, status.
Never prints key values. Exit 1 on an Anthropic or Static Maps failure; a non-OK Street
View metadata status is only a warning (the pipeline degrades to satellite-only).

Run: uv run python -m hero.tools.check_keys
"""

from __future__ import annotations

import sys

import anthropic
import httpx

from hero.config import ConfigError, get_settings

SEED_START = (12.97400, 77.64050)  # 100 Feet Road, Indiranagar - west footpath start
STATIC_MAPS_URL = "https://maps.googleapis.com/maps/api/staticmap"
STREETVIEW_META_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"


def check_anthropic(settings) -> bool:
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        default_headers={"anthropic-workspace-id": settings.anthropic_workspace_id.get_secret_value()},
        timeout=60.0,
        max_retries=1,
    )
    try:
        msg = client.messages.create(
            model=settings.vision_model,
            max_tokens=5,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
        print(f"anthropic: model={settings.vision_model} ok=True usage_in={msg.usage.input_tokens} usage_out={msg.usage.output_tokens}")
        return True
    except anthropic.APIStatusError as exc:
        print(f"anthropic: model={settings.vision_model} ok=False status={exc.status_code} type={type(exc).__name__}")
        if exc.status_code in (401, 403, 400):
            print("  hint: check HERO_ANTHROPIC_API_KEY / HERO_ANTHROPIC_WORKSPACE_ID in .env")
        if exc.status_code == 404:
            print("  hint: model slug not found - check HERO_VISION_MODEL")
        return False
    except anthropic.APIConnectionError as exc:
        print(f"anthropic: model={settings.vision_model} ok=False connection_error={type(exc).__name__}")
        return False


def check_static_maps(settings) -> bool:
    lat, lon = SEED_START
    params = {
        "center": f"{lat},{lon}", "zoom": str(settings.satellite_zoom), "size": "640x640",
        "scale": "2", "maptype": "satellite", "key": settings.google_maps_api_key.get_secret_value(),
    }
    try:
        resp = httpx.get(STATIC_MAPS_URL, params=params, timeout=20.0)
    except httpx.HTTPError as exc:
        print(f"static_maps: ok=False error={type(exc).__name__}")
        return False
    is_png = resp.headers.get("content-type", "").startswith("image/")
    ok = resp.status_code == 200 and is_png
    print(f"static_maps: status={resp.status_code} bytes={len(resp.content)} image={is_png} ok={ok}")
    if not ok:
        print("  hint: check HERO_GOOGLE_MAPS_API_KEY and that the Maps Static API is enabled")
    return ok


def check_streetview_metadata(settings) -> str:
    lat, lon = SEED_START
    params = {"location": f"{lat},{lon}", "radius": "50", "source": "outdoor",
              "key": settings.google_maps_api_key.get_secret_value()}
    try:
        resp = httpx.get(STREETVIEW_META_URL, params=params, timeout=20.0)
        status = resp.json().get("status", f"HTTP {resp.status_code}")
    except (httpx.HTTPError, ValueError) as exc:
        status = f"error:{type(exc).__name__}"
    label = "ok" if status == "OK" else "WARNING (non-OK: pipeline will run satellite-only)"
    print(f"streetview_metadata: status={status} {label}")
    return status


def main() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"config: {exc}")
        return 1
    print(f"keys present: anthropic={bool(settings.anthropic_api_key.get_secret_value())} "
          f"workspace_id={bool(settings.anthropic_workspace_id.get_secret_value())} "
          f"google={bool(settings.google_maps_api_key.get_secret_value())} "
          f"admin_password={bool(settings.admin_password.get_secret_value())}")
    a = check_anthropic(settings)
    g = check_static_maps(settings)
    check_streetview_metadata(settings)
    print(f"result: {'PASS' if a and g else 'FAIL'}")
    return 0 if a and g else 1


if __name__ == "__main__":
    sys.exit(main())
