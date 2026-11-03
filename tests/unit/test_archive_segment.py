"""hero.tools.archive_segment: archive hides a street everywhere but keeps its rows and spend; restore undoes it."""

from __future__ import annotations

from hero.db import repo
from hero.fusion.recompile import recompile_slugs
from hero.scene.schema import Quality, SegmentStatus, load_example_scene
from hero.tools import archive_segment
from tests.unit.test_repo import _segment_create


def test_archive_hides_the_street_and_restore_brings_it_back(test_db, capsys):
    seg = repo.create_segment(_segment_create("Archive Me Road"))
    run = repo.create_run(seg.id, {})
    repo.finalize_segment_scene(seg.id, run_id=run.id, scene=load_example_scene(), quality=Quality.full, thumbnail_path=None)
    assert seg.slug in [s.slug for s in repo.list_ready_segments()]

    assert archive_segment.main(["--slug", seg.slug]) == 0
    assert repo.get_segment(seg.slug).status is SegmentStatus.archived
    assert seg.slug not in [s.slug for s in repo.list_ready_segments()]
    assert seg.slug not in recompile_slugs(include_legacy=True)
    assert repo.get_run(run.id) is not None          # nothing deleted
    assert "-> archived" in capsys.readouterr().out

    assert archive_segment.main(["--slug", seg.slug, "--restore"]) == 0
    assert repo.get_segment(seg.slug).status is SegmentStatus.ready


def test_unknown_slug_exits_1(test_db, capsys):
    assert archive_segment.main(["--slug", "no-such-street"]) == 1
    assert "not found" in capsys.readouterr().out
