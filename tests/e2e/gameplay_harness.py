"""Tiny static harness for the S3 gameplay tests: serves the repo's ``frontend/`` dir, ``/api/gameplay.json``
(from ``src/hero/export/gameplay.json``) and a blank ``/__harness.html`` page, so the JS modules are loaded
exactly as the browser game loads them without depending on ``main.js`` or the hero app.
"""

from __future__ import annotations

import atexit
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
GAMEPLAY_PATH = ROOT / "src" / "hero" / "export" / "gameplay.json"
SPARE_PORTS = (8745, 8746, 8747, 8748)
PHONE_VIEWPORT = {"width": 390, "height": 844}
CHROMIUM_ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
HTML = b"<!doctype html><html><head><meta charset=utf-8><title>harness</title></head><body><canvas id=c width=390 height=844></canvas></body></html>"


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__harness.html":
            return self._send(HTML, "text/html")
        if path == "/api/gameplay.json":
            return self._send(GAMEPLAY_PATH.read_bytes(), "application/json")
        for prefix, root in (("/fx/", ROOT / "tests" / "e2e" / "fixtures"), ("/fixtures/", ROOT / "tests" / "fixtures")):
            if path.startswith(prefix):
                f = (root / path[len(prefix):]).resolve()
                if root in f.parents and f.is_file():
                    return self._send(f.read_bytes(), "application/json")
                self.send_error(404)
                return None
        return super().do_GET()

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _start() -> tuple[ThreadingHTTPServer, str]:
    handler = partial(_Handler, directory=str(FRONTEND))
    for port in (*SPARE_PORTS, 0):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
            break
        except OSError:
            continue
    else:  # pragma: no cover
        raise RuntimeError("no free port for the gameplay harness")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# One server / one browser per pytest process, shared by every test module that imports these fixtures (fixture
# objects imported into several modules would otherwise be instantiated once per module, and two sync Playwright
# drivers cannot coexist in one process).
_state: dict = {}


@pytest.fixture(scope="session")
def harness_url():
    if "url" not in _state:
        _state["srv"], _state["url"] = _start()
    return _state["url"]


@pytest.fixture(scope="session")
def gameplay() -> dict:
    return json.loads(GAMEPLAY_PATH.read_text())


@pytest.fixture(scope="session")
def harness_browser() -> Browser:
    if "browser" not in _state:
        _state["pw"] = sync_playwright().start()
        _state["browser"] = _state["pw"].chromium.launch(headless=True, args=CHROMIUM_ARGS)
    return _state["browser"]


def _cleanup() -> None:
    if "browser" in _state:
        _state.pop("browser").close()
        _state.pop("pw").stop()
    if "srv" in _state:
        srv = _state.pop("srv")
        srv.shutdown()
        srv.server_close()


atexit.register(_cleanup)


@pytest.fixture()
def hpage(harness_browser: Browser, harness_url: str) -> Page:
    ctx = harness_browser.new_context(viewport=PHONE_VIEWPORT)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(harness_url + "/__harness.html")
    pg.hero_errors = errors  # type: ignore[attr-defined]
    pg.hero_url = harness_url  # type: ignore[attr-defined]
    yield pg
    ctx.close()
