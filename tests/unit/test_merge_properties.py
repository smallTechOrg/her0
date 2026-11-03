"""Property-style tests for hero.model.merge (seeded random scenarios; no extra dependency).

Properties: idempotence, order independence, stable entity ids across versions, never silently dropping
anything, confidence rises with supporting and falls with contradicting evidence, contradictions stay visible,
evidence uniqueness.
"""

from __future__ import annotations

import math
import random

import pytest

from hero.model.batch import ProjectedBatch, ProjectedDetection, StateSpan, StripSample, VisibleRegion
from hero.model.calibrate import confidence_from_logit
from hero.model.diff import model_hash
from hero.model.merge import merge_batches
from hero.model.types import (
    CanonicalModel,
    Condition,
    ContradictionKind,
    EntityState,
    EntityType,
    EvidenceRole,
    FootpathState,
    Material,
    StripKind,
)
from tests.fixtures.street_fixtures import FIXED_NOW, make_model, straight_reference_line

SEEDS = list(range(25))
TYPES = [EntityType.pole, EntityType.tree, EntityType.bollard, EntityType.parked_vehicle, EntityType.other]


def base():
    return make_model(straight_reference_line(100.0))


def merge(m, bs, v=None):
    return merge_batches(m, bs, new_version=m.version + 1 if v is None else v, run_id=None, now=FIXED_NOW)


def random_batches(rng: random.Random, n_batches: int, prefix: str = "o") -> list[ProjectedBatch]:
    spots = [(rng.uniform(5, 95), rng.choice([-5.5, 5.5, 6.0])) for _ in range(8)]
    out = []
    for b in range(n_batches):
        obs = [f"{prefix}{b}-{i}" for i in range(rng.randint(1, 3))]
        dets, vis, strips, states = [], [], [], []
        for o in obs:
            for k in range(rng.randint(0, 5)):
                s, t = rng.choice(spots)
                dets.append(ProjectedDetection(
                    key=f"k{k}", observation_id=o, type=rng.choice(TYPES), subtype=None,
                    s=s + rng.gauss(0, 0.3), t=t + rng.gauss(0, 0.3), sigma_m=rng.uniform(0.4, 1.5), extent_s=0.5,
                    extent_t=0.5, height_m=None, orientation_deg=None,
                    attributes={"colour": rng.choice(["white", "red"])} if rng.random() < 0.3 else {},
                    description=f"thing {k}", raw_confidence=rng.random(), crop_px=None, weight=rng.uniform(0.3, 1.0)))
            a = rng.uniform(0, 70)
            vis.append(VisibleRegion(o, a, a + rng.uniform(10, 30), rng.choice(["left", "right", "both"]), rng.uniform(0.4, 1.0)))
            for _ in range(rng.randint(0, 3)):
                st = rng.uniform(0, 100)
                strips.append(StripSample(st, StripKind.footpath, "left", 4.0 + rng.gauss(0, .1), 5.5 + rng.gauss(0, .1),
                                          Material.paver, Condition.fair, rng.uniform(0.5, 1.2), o))
            if rng.random() < 0.5:
                a = rng.uniform(0, 80)
                states.append(StateSpan(o, "left", a, a + rng.uniform(5, 20),
                                        rng.choice([FootpathState.intact, FootpathState.broken, FootpathState.missing]),
                                        rng.uniform(0.5, 1.0)))
        out.append(ProjectedBatch(batch_id=f"{prefix}-b{b}", observation_ids=tuple(obs), detections=tuple(dets),
                                  strips=tuple(strips), states=tuple(states), visible=tuple(vis)))
    return out


def valid(m: CanonicalModel) -> None:
    CanonicalModel.model_validate(m.model_dump(mode="json"))
    for e in m.entities:
        assert 0.02 <= e.confidence <= 0.98
        assert e.confidence == pytest.approx(confidence_from_logit(e.logit), abs=1e-9) or e.provenance[-1].role is EvidenceRole.critique
        keys = [(x.observation_id, x.detection_key) for x in e.provenance if x.detection_key is not None]
        assert len(keys) == len(set(keys)), "evidence must be unique per (observation_id, detection_key)"
        absent = [x.observation_id for x in e.contradictions()]
        assert len(absent) == len(set(absent)), "absence is unique per observation"
    for iv in m.profile:
        assert 2.5 - 1e-6 <= iv.length <= 30.0 + 1e-6
        if iv.footpath_state is FootpathState.missing:
            assert iv.footpath_strip("left") is None


@pytest.mark.parametrize("seed", SEEDS)
def test_idempotence(seed):
    rng = random.Random(seed)
    bs = random_batches(rng, rng.randint(1, 4))
    once = merge(base(), bs)
    valid(once.model)
    twice = merge(once.model, bs)
    assert model_hash(twice.model) == model_hash(once.model)
    assert sorted(twice.ignored_batches) == sorted(b.batch_id for b in bs)
    assert twice.changes == []
    assert twice.model.version == 2 and twice.model.parent_version == 1
    # in-call duplicates are also no-ops
    dup = merge(base(), bs + bs)
    assert model_hash(dup.model) == model_hash(once.model)


@pytest.mark.parametrize("seed", SEEDS)
def test_batch_order_independence(seed):
    rng = random.Random(1000 + seed)
    bs = random_batches(rng, 4)
    ref = model_hash(merge(base(), bs).model)
    for _ in range(3):
        shuffled = bs[:]
        rng.shuffle(shuffled)
        assert model_hash(merge(base(), shuffled).model) == ref


@pytest.mark.parametrize("seed", SEEDS)
def test_entity_ids_are_stable_across_versions_and_replays(seed):
    rng = random.Random(2000 + seed)
    bs = random_batches(rng, 5)
    m, history = base(), []
    for i, b in enumerate(bs):
        m = merge(m, [b]).model
        history.append(m)
    for prev, nxt in zip(history, history[1:]):
        prev_ids = [e.id for e in prev.entities]
        assert [e.id for e in nxt.entities][: len(prev_ids)] == prev_ids  # same ids, same order, appended after
        for e in prev.entities:
            assert nxt.entity(e.id).first_seen_version == e.first_seen_version
    # replaying the same batches onto the same base yields identical ids
    again = base()
    for b in bs:
        again = merge(again, [b]).model
    assert [e.id for e in again.entities] == [e.id for e in history[-1].entities]
    assert len({e.id for e in history[-1].entities}) == len(history[-1].entities)


@pytest.mark.parametrize("seed", SEEDS)
def test_nothing_is_silently_dropped(seed):
    rng = random.Random(3000 + seed)
    m = base()
    seen_e, seen_b, seen_c = set(), set(), set()
    for b in random_batches(rng, 6):
        r = merge(m, [b])
        m = r.model
        valid(m)
        ids_e, ids_c = {e.id for e in m.entities}, {c.id for c in m.contradictions}
        assert seen_e <= ids_e and seen_c <= ids_c and seen_b <= {x.id for x in m.buildings}
        seen_e, seen_c, seen_b = ids_e, ids_c, {x.id for x in m.buildings}
        assert m.stats.entity_count + m.stats.removed_count == len(m.entities)
        # every detection of the batch is accounted for: as support evidence of some entity
        for d in b.detections:
            assert any((x.observation_id, x.detection_key) == (d.observation_id, d.key) for e in m.entities for x in e.provenance)
    # an entity with strong contradiction evidence is marked removed, never deleted
    seeded_m = merge(base(), [ProjectedBatch("s", ("s0",), (ProjectedDetection(
        "k", "s0", EntityType.pole, None, 20, 5.5, 0.5, .3, .3, None, None, {}, "p", 0.2, None, 1.0),))]).model
    for i in range(8):
        seeded_m = merge(seeded_m, [ProjectedBatch(f"a{i}", (f"a{i}",), visible=(VisibleRegion(f"a{i}", 0, 100, "both", 1.0),))]).model
    assert len(seeded_m.entities) == 1 and seeded_m.entities[0].state is EntityState.removed


def one_det(obs, raw, key="k", s=20.0):
    return ProjectedDetection(key, obs, EntityType.pole, None, s, 5.5, 0.5, .3, .3, None, None, {}, "pole", raw, None, 1.0)


@pytest.mark.parametrize("seed", SEEDS)
def test_confidence_rises_with_support_and_falls_with_absence(seed):
    rng = random.Random(4000 + seed)
    raw = rng.uniform(0.7, 1.0)  # calibrated p > 0.5 needs raw >= ~0.62  # same score each time, so the decay of repeat evidence is the only variable
    m = merge(base(), [ProjectedBatch("b0", ("o0",), (one_det("o0", raw),))]).model
    eid = m.entities[0].id
    confs, logits = [m.entities[0].confidence], [m.entities[0].logit]
    for i in range(1, 5):  # supportive sightings (calibrated p > 0.5): confidence never falls
        m = merge(m, [ProjectedBatch(f"b{i}", (f"o{i}",), (one_det(f"o{i}", raw, s=20 + rng.uniform(-.4, .4)),))]).model
        confs.append(m.entity(eid).confidence)
        logits.append(m.entity(eid).logit)
    assert all(b >= a - 1e-12 for a, b in zip(confs, confs[1:])) and confs[-1] > confs[0]
    gains = [b - a for a, b in zip(logits, logits[1:])]
    assert all(g2 <= g1 + 1e-9 for g1, g2 in zip(gains, gains[1:]))  # diminishing returns
    peak = confs[-1]
    prev = peak
    for i in range(5):  # contradicting sightings: confidence never rises
        m = merge(m, [ProjectedBatch(f"c{i}", (f"oc{i}",), visible=(VisibleRegion(f"oc{i}", 0, 100, "both", rng.uniform(.5, 1)),))]).model
        cur = m.entity(eid).confidence
        assert cur <= prev + 1e-12
        prev = cur
    assert prev < peak


@pytest.mark.parametrize("seed", SEEDS)
def test_contradictions_stay_visible(seed):
    rng = random.Random(5000 + seed)
    m = base()
    opened_ever: dict[str, int] = {}
    for b in random_batches(rng, 6):
        m = merge(m, [b]).model
        by_id = {c.id: c for c in m.contradictions}
        for cid in opened_ever:
            assert cid in by_id, "a contradiction, once opened, is never removed from the model"
            assert by_id[cid].opened_in_version == opened_ever[cid]
        for c in m.contradictions:
            opened_ever.setdefault(c.id, c.opened_in_version)
        # consistency: disputed entities always have an open presence conflict
        for e in m.entities:
            if e.state is EntityState.disputed:
                assert any(c.is_open and c.kind is ContradictionKind.presence_conflict and c.entity_ids == [e.id]
                           for c in m.contradictions)
        # confident, close, differently-typed entities always have an open type conflict
        live = [e for e in m.entities if e.state is not EntityState.removed and e.confidence >= 0.5]
        for i, a in enumerate(live):
            for c in live[i + 1:]:
                if a.type != c.type and EntityType.other not in (a.type, c.type) and \
                        math.hypot(a.position.s - c.position.s, a.position.t - c.position.t) <= 1.2:
                    assert any(x.is_open and x.kind is ContradictionKind.type_conflict and set(x.entity_ids) == {a.id, c.id}
                               for x in m.contradictions)


def test_weak_sightings_lower_confidence():
    """A sighting whose calibrated score is below 0.5 is evidence *against* (log-odds < 0), so confidence falls."""
    m = merge(base(), [ProjectedBatch("b0", ("o0",), (one_det("o0", 0.9),))]).model
    before = m.entities[0].confidence
    m = merge(m, [ProjectedBatch("b1", ("o1",), (one_det("o1", 0.1),))]).model
    assert m.entities[0].confidence < before
