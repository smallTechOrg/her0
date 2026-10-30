// Path / corridor geometry (pure, no Three.js). One Track serves both scene schemas:
//   v2: samples + stations come from scene.path / scene.corridor (traced road, per-station lane centres);
//   v1: a straight synthetic track (theta = 0, constant lane centres from cross_section) so the rest of the
//       game has a single code path and v1 scenes behave exactly as before.
// World frame (spec/data.md "Scene JSON v2"): runner advances toward -z, footpath side is +x.
//   forward f(theta) = (sin t, -cos t), footpath-side normal n(theta) = (cos t, sin t), position(s, lat) = P(s) + lat * n.
// A rigid copy of the segment chained at the end starts at pose (P(L), theta(L)); the lane/strip blend baked into
// the last 6 m by the compiler makes the seam continuous.

export const STEP_M = 2.5;
export const V1_GROUND_HALF_M = 40;

const TAU = Math.PI * 2;
const wrapPi = (a) => { let r = a % TAU; if (r > Math.PI) r -= TAU; if (r <= -Math.PI) r += TAU; return r; };

/** Rotation R(a) used by chained copies: maps forward (0,-1) -> f(a) and normal (1,0) -> n(a). */
export function rotate(a, x, z) {
  const c = Math.cos(a), s = Math.sin(a);
  return [x * c - z * s, x * s + z * c];
}

function v1Stations(scene) {
  const L = scene.segment.length_m;
  const lanes = scene.cross_section.lanes;
  const half = V1_GROUND_HALF_M;
  const { frame_to_px: f, size_px } = scene.ground;
  const uvAt = (s, lat) => [
    (f.origin_px[0] + s * f.along_px_per_m[0] + lat * f.across_px_per_m[0]) / size_px[0],
    (f.origin_px[1] + s * f.along_px_per_m[1] + lat * f.across_px_per_m[1]) / size_px[1],
  ];
  const sVals = [];
  for (let s = 0; s < L - 1e-6; s += STEP_M) sVals.push(s);
  sVals.push(L);
  const stateAt = (s) => {
    const r = scene.cross_section.footpath_coverage.find((c) => s >= c.from_m - 1e-9 && s < c.to_m) ||
      scene.cross_section.footpath_coverage[scene.cross_section.footpath_coverage.length - 1];
    return r.state;
  };
  const stations = sVals.map((s, i) => {
    // The state of a quad [s_i, s_i+1] is taken at its midpoint (stations carry the state forward).
    const mid = Math.min(L, s + (i + 1 < sVals.length ? (sVals[i + 1] - s) / 2 : 0));
    const state = stateAt(mid);
    const w = lanes[0].width_m;
    return {
      s,
      lane_lat: [lanes[0].lat_m, lanes[1].lat_m, lanes[2].lat_m],
      footpath: state === 'missing' ? null
        : { lo: lanes[0].lat_m - w / 2, hi: lanes[0].lat_m + w / 2, state, height_m: 0.12, confidence: 1 },
      strips: [],
      ground_uv: [uvAt(s, -half), uvAt(s, half)],
    };
  });
  const samples = sVals.map((s) => ({ s, x: 0, z: -s, theta: 0 }));
  return { samples, stations, half };
}

export class Track {
  constructor(scene) {
    this.v2 = scene.schema_version === 2;
    this.length = scene.segment.length_m;
    if (this.v2) {
      this.samples = scene.path.samples;
      this.stations = scene.corridor.stations;
      this.groundHalf = scene.corridor.ground_half_m;
    } else {
      const v1 = v1Stations(scene);
      this.samples = v1.samples;
      this.stations = v1.stations;
      this.groundHalf = v1.half;
    }
    const last = this.samples[this.samples.length - 1];
    this.end = { x: last.x, z: last.z, theta: last.theta };
    this.step = scene.corridor ? scene.corridor.step_m : STEP_M;
  }

  /** Index i such that samples[i].s <= s < samples[i+1].s (clamped), and the fraction within. */
  _locate(s) {
    const a = this.samples;
    let lo = 0, hi = a.length - 1;
    if (s <= 0) return [0, 0];
    if (s >= a[hi].s) return [hi - 1, 1];
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (a[mid].s <= s) lo = mid; else hi = mid;
    }
    return [lo, (s - a[lo].s) / (a[lo + 1].s - a[lo].s)];
  }

  wrap(s) { const L = this.length; return ((s % L) + L) % L; }

  /** Pose within one copy, s clamped to [0, L]. */
  localPose(s) {
    const [i, t] = this._locate(s);
    const p = this.samples[i], q = this.samples[i + 1];
    return { x: p.x + (q.x - p.x) * t, z: p.z + (q.z - p.z) * t, theta: p.theta + (q.theta - p.theta) * t };
  }

  /** Pose for any real s (chained copies before/after the segment). */
  pose(s) {
    const L = this.length;
    const k = Math.floor(s / L);
    const p = this.localPose(s - k * L);
    return this.applyCopy(p, k);
  }

  /** Apply the copy transform k times (k < 0: inverse). */
  applyCopy(p, k) {
    let { x, z, theta } = p;
    const e = this.end;
    for (let j = 0; j < k; j++) {
      const [rx, rz] = rotate(e.theta, x, z);
      x = e.x + rx; z = e.z + rz; theta += e.theta;
    }
    for (let j = 0; j > k; j--) {
      const [rx, rz] = rotate(-e.theta, x - e.x, z - e.z);
      x = rx; z = rz; theta -= e.theta;
    }
    return { x, z, theta };
  }

  /** World position of (s, lat) plus the path heading theta (radians) there. */
  world(s, lat) {
    const p = this.pose(s);
    return { x: p.x + lat * Math.cos(p.theta), z: p.z + lat * Math.sin(p.theta), theta: p.theta };
  }

  /** Heading in degrees (wrapped to (-180, 180]) - 0 = along -z. */
  headingDeg(s) { return wrapPi(this.pose(s).theta) * 180 / Math.PI; }

  _stationPair(s) {
    const [i, t] = this._locate(this.wrap(s));
    return [this.stations[i], this.stations[i + 1], t];
  }

  laneLat(s, lane) {
    const [a, b, t] = this._stationPair(s);
    return a.lane_lat[lane] + (b.lane_lat[lane] - a.lane_lat[lane]) * t;
  }

  /** Traced footpath strip at s: {lo, hi, centre, state} or null in a missing gap. */
  footpathAt(s) {
    const [a, b, t] = this._stationPair(s);
    const near = t < 0.5 ? a : b;
    if (!near.footpath) return null;
    const fa = a.footpath || near.footpath, fb = b.footpath || near.footpath;
    const lo = fa.lo + (fb.lo - fa.lo) * t, hi = fa.hi + (fb.hi - fa.hi) * t;
    return { lo, hi, centre: (lo + hi) / 2, state: near.footpath.state, height_m: near.footpath.height_m };
  }

  /** Which strip is under lateral position lat at s. */
  stripAt(s, lat) {
    const [a, b, t] = this._stationPair(s);
    const near = t < 0.5 ? a : b;
    const fp = this.footpathAt(s);
    if (fp && lat >= fp.lo && lat <= fp.hi) return 'footpath';
    if (fp && lat < fp.lo && lat >= fp.lo - 0.3) return 'kerb';
    for (const st of near.strips) if (lat >= st.lo && lat <= st.hi) return st.kind;
    if (!fp) {
      const edge = near.strips.length ? Math.max(...near.strips.map((x) => x.hi)) : near.lane_lat[1];
      if (lat >= edge) return 'gap';
    }
    return 'ground';
  }
}

export function makeTrack(scene) { return new Track(scene); }
