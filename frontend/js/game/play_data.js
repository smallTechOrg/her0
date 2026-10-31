// play_data.js: the ONLY place that knows both worlds. Turns a manifest (v3) or a legacy v1/v2 scene into the
// frame-agnostic `playData` the Runner / spawner / dynamic layer consume:
//   { slug, source: 'manifest'|'legacy', length_m, track, colliders, tokens, spawnInput, lanes }
//   colliders[i] = { id, entity_id, type, label, s, x_m, length_m, lanes[], action, size_m, confidence, ... }   (s === x_m)
//   tokens[i]    = { id, s, x_m, lane, reason }                                                              (s === x_m)
//   spawnInput   = { slug, length_m, density, type_weights, footpath_missing:[[s0,s1]], static:[{s0,s1,lanes,action}],
//                    keepout:[[a,b]] }   (spec/portability.md §7; `seed` is supplied by the runner)
//   lanes        = [{ index, name, kind, width_m }]
import { ManifestTrack } from '/js/game/track_manifest.js?v=1';

const STATION_HALF_M = 1.25;
const KEEPOUT_TYPES = new Set(['crossing', 'ramp']);

/** keepout margin from gameplay.json (spawner.keepout_margin_m); a missing value is a hard error, never a guess. */
function keepoutMarginOf(gameplay, where) {
  const m = gameplay && gameplay.spawner && gameplay.spawner.keepout_margin_m;
  if (typeof m !== 'number') throw new Error(`play_data: gameplay.spawner.keepout_margin_m missing (${where})`);
  return m;
}

/** Ranges [s - 1.25, s + 1.25] of every station whose footpath is null, adjacent ranges merged (portability §7). */
export function footpathMissingRanges(stations) {
  const out = [];
  for (const st of stations) {
    if (st.footpath) continue;
    const a = st.s - STATION_HALF_M, b = st.s + STATION_HALF_M;
    const last = out[out.length - 1];
    if (last && a <= last[1] + 1e-9) last[1] = Math.max(last[1], b); else out.push([a, b]);
  }
  return out;
}

function buildSpawnInput({ slug, length_m, hints, stations, colliders, keepoutMargin }) {
  return {
    slug,
    length_m,
    density: hints.density,
    type_weights: { ...hints.type_weights },
    footpath_missing: footpathMissingRanges(stations),
    static: colliders.filter((c) => c.action !== 'none').map((c) => ({ s0: c.s, s1: c.s + c.length_m, lanes: [...c.lanes], action: c.action })),
    keepout: colliders.filter((c) => KEEPOUT_TYPES.has(c.type)).map((c) => [c.s - keepoutMargin, c.s + c.length_m + keepoutMargin]),
  };
}

export function fromManifest(manifest) {
  const track = new ManifestTrack(manifest);
  const slug = manifest.segment.slug;
  const colliders = manifest.colliders.map((c) => ({ ...c, x_m: c.s }));
  const tokens = manifest.tokens.map((t) => ({ ...t, x_m: t.s }));
  const keepoutMargin = keepoutMarginOf(manifest.gameplay, 'manifest.gameplay');
  return {
    slug, source: 'manifest', length_m: track.length, track, colliders, tokens,
    lanes: manifest.lanes.defs.map((d) => ({ ...d })),
    spawnInput: buildSpawnInput({ slug, length_m: track.length, hints: manifest.spawn_hints, stations: track.stations, colliders, keepoutMargin }),
  };
}

/**
 * Legacy v1/v2 scene. `track` is the legacy path.js Track (mirrored frame); `gameplay` supplies the default spawn
 * hints (these streets have no manifest).
 */
export function fromLegacyScene(scene, track, gameplay) {
  const slug = scene.segment.slug;
  const colliders = scene.objects.map((o) => ({
    id: o.id, entity_id: o.entity_id || o.id, type: o.type, label: o.label, s: o.x_m, x_m: o.x_m, length_m: o.length_m,
    lanes: [...o.lanes], action: o.action, size_m: o.size_m, confidence: o.confidence,
  }));
  const tokens = scene.tokens.map((t) => ({ id: t.id, s: t.x_m, x_m: t.x_m, lane: t.lane, reason: t.reason }));
  const sp = (gameplay && gameplay.spawner) || {};
  const hints = sp.default_spawn_hints || { density: 0.5, type_weights: {} };
  const keepoutMargin = keepoutMarginOf(gameplay, 'legacy gameplay');
  return {
    slug, source: 'legacy', length_m: scene.segment.length_m, track, colliders, tokens,
    lanes: scene.cross_section.lanes.map((l) => ({ index: l.index, name: l.name, kind: l.kind, width_m: l.width_m })),
    spawnInput: buildSpawnInput({ slug, length_m: scene.segment.length_m, hints, stations: track.stations, colliders, keepoutMargin }),
  };
}

/**
 * The scene object handed to the results card. A v3 (glb) scene is "lite": the detected-object list and the hazard map
 * markers live in the manifest, so this adds the legacy-shaped `objects` / `tokens` views derived from the manifest
 * (colliders joined with their entity descriptions). Legacy scenes pass through unchanged.
 */
export function resultsScene(scene, manifest) {
  if (!manifest || scene.objects) return scene;
  const ents = new Map((manifest.entities || []).map((e) => [e.id, e]));
  const objects = manifest.colliders.map((c) => {
    const e = c.entity_id ? ents.get(c.entity_id) : null;
    return {
      id: c.id, entity_id: c.entity_id, type: c.type, label: c.label, x_m: c.s, length_m: c.length_m, lanes: [...c.lanes],
      action: c.action, size_m: c.size_m, confidence: c.confidence, latlon: c.latlon, tile_px: c.tile_px,
      description: e ? e.description : '', evidence: null, source: null, billboard_url: null, scenery: false,
    };
  });
  const tokens = manifest.tokens.map((t) => ({ ...t, x_m: t.s }));
  return { ...scene, objects, tokens };
}
