"""hero.imagery.cache: content-addressed get_or_fetch (hit/miss, key stability, no secrets)."""

from __future__ import annotations

import json

import pytest

from hero.imagery.cache import ImageryCache


@pytest.fixture()
def cache(tmp_path):
    return ImageryCache(tmp_path / "imagery")


def test_first_fetch_misses_writes_file_and_sidecar(cache):
    calls = []

    def fetch() -> bytes:
        calls.append(1)
        return b"\x89PNG fake"

    entry = cache.get_or_fetch("satellite", {"center": "12.974,77.6405", "zoom": 19}, fetch, suffix="png")

    assert entry.hit is False
    assert entry.path.read_bytes() == b"\x89PNG fake"
    assert entry.path.name == f"{entry.key}.png"
    sidecar = json.loads(cache.sidecar_for(entry.path).read_text())
    assert sidecar == {"kind": "satellite", "params": {"center": "12.974,77.6405", "zoom": 19}}
    assert calls == [1]


def test_second_fetch_with_same_params_hits_without_calling_fetch(cache):
    params = {"center": "12.974,77.6405", "zoom": 19}
    cache.get_or_fetch("satellite", params, lambda: b"first", suffix="png")

    def must_not_run() -> bytes:
        raise AssertionError("fetch must not run on a cache hit")

    entry = cache.get_or_fetch("satellite", params, must_not_run, suffix="png")

    assert entry.hit is True
    assert entry.path.read_bytes() == b"first"


def test_key_is_independent_of_dict_insertion_order_and_depends_on_kind(cache):
    a = cache.key("satellite", {"zoom": 19, "center": "x"})
    b = cache.key("satellite", {"center": "x", "zoom": 19})
    c = cache.key("streetview", {"center": "x", "zoom": 19})

    assert a == b
    assert a != c
    assert len(a) == 64


def test_different_params_produce_different_files(cache):
    one = cache.get_or_fetch("streetview", {"location": "a", "heading": 0}, lambda: b"a", suffix="jpg")
    two = cache.get_or_fetch("streetview", {"location": "a", "heading": 90}, lambda: b"b", suffix="jpg")

    assert one.path != two.path
    assert one.path.read_bytes() == b"a" and two.path.read_bytes() == b"b"


def test_failed_fetch_leaves_no_partial_file_so_next_attempt_misses_again(cache):
    params = {"location": "boom"}

    def failing() -> bytes:
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        cache.get_or_fetch("streetview", params, failing, suffix="jpg")

    assert not cache.path_for("streetview", params, "jpg").exists()
    entry = cache.get_or_fetch("streetview", params, lambda: b"ok", suffix="jpg")
    assert entry.hit is False


def test_empty_fetch_result_is_rejected_and_not_cached(cache):
    with pytest.raises(ValueError):
        cache.get_or_fetch("satellite", {"x": 1}, lambda: b"", suffix="png")
    assert not cache.path_for("satellite", {"x": 1}, "png").exists()
