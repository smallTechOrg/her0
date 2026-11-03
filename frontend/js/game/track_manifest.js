// Track built from street.manifest.json (spec/portability.md §2, §4). Same public API as the legacy path.js Track
// (world(s, lat), laneLat(s, lane), footpathAt(s), stripAt(s, lat), applyCopy(p, k), headingDeg(s), length), so
// runner.js / the dynamic layer are frame-agnostic.
//
// FRAME. The glb and this Track live in the TRUE, unmirrored street frame: travel is toward -Z at s = 0, +X is the
// RIGHT of travel, `t` (lateral) is positive to the LEFT of travel and
//   world(s, t) = (P.x(s) - t cos(phi(s)), P.z(s) - t sin(phi(s)))      with forward f = (sin phi, -cos phi).
// The legacy runner calls the lateral coordinate `lat` (legacy frame: +x = footpath side, mirrored). Here `lat` IS
// `t`: laneLat() returns the manifest's lane-centre t (left-positive, so a left footpath is +t), world() takes t, and
// every lat -> t conversion lives in this file (latToT / tToLat are the identity on purpose and tested as such).
// A footpath on the right simply has negative lane_t[0].

const TAU = Math.PI * 2;
const wrapPi = (a) => { let r = a % TAU; if (r > Math.PI) r -= TAU; if (r <= -Math.PI) r += TAU; return r; };

/** Rotation used by chained copies: forward (0,-1) -> (sin a, -cos a), i.e. THREE's rotation.y = -a. */
export function rotate(a, x, z) {
  const c = Math.cos(a), s = Math.sin(a);
  return [x * c - z * s, x * s + z * c];
}

export class ManifestTrack {
  constructor(manifest) {
    this.manifest = manifest;
    this.frame = 'true';
    this.length = manifest.reference_line.length_s;
    const b0 = manifest.reference_line.points[0].bearing_deg;
    this.samples = manifest.reference_line.points.map((p) => ({
      s: p.s, x: p.x, z: p.z, theta: wrapPi((p.bearing_deg - b0) * Math.PI / 180),
    }));
    // unwrap theta so interpolation never jumps by 2*pi
    for (let i = 1; i < this.samples.length; i++) {
      const d = wrapPi(this.samples[i].theta - this.samples[i - 1].theta);
      this.samples[i].theta = this.samples[i - 1].theta + d;
    }
    // stations are index-aligned with the reference-line points
    this.stations = manifest.lanes.stations.map((st) => ({
      s: st.s,
      lane_lat: st.lane_t,
      footpath: st.footpath ? { lo: st.footpath.t_lo, hi: st.footpath.t_hi, state: st.footpath.state,
        height_m: st.footpath.height_m, confidence: st.footpath.confidence } : null,
      kerb: st.kerb || null,
    }));
    this.cross = manifest.cross_section ? manifest.cross_section.stations : [];
    const e = manifest.loop.end_pose;
    this.end = { x: e.x, z: e.z, theta: e.yaw_rad };
    this.footpathSide = manifest.segment.footpath_side;
  }

  latToT(lat) { return lat; }
  tToLat(t) { return t; }

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

  localPose(s) {
    const [i, t] = this._locate(s);
    const p = this.samples[i], q = this.samples[i + 1];
    return { x: p.x + (q.x - p.x) * t, z: p.z + (q.z - p.z) * t, theta: p.theta + (q.theta - p.theta) * t };
  }

  pose(s) {
    const L = this.length;
    const k = Math.floor(s / L);
    return this.applyCopy(this.localPose(s - k * L), k);
  }

  /** Apply the copy transform k times (k < 0: inverse). Identical maths to the legacy Track. */
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

  /** World position of (s, t) plus the heading theta there. t is positive to the LEFT of travel. */
  world(s, lat) {
    const p = this.pose(s);
    return { x: p.x - lat * Math.cos(p.theta), z: p.z - lat * Math.sin(p.theta), theta: p.theta };
  }

  headingDeg(s) { return wrapPi(this.pose(s).theta) * 180 / Math.PI; }

  _stationPair(s) {
    const [i, t] = this._locate(this.wrap(s));
    return [this.stations[i], this.stations[i + 1], t, i];
  }

  laneLat(s, lane) {
    const [a, b, t] = this._stationPair(s);
    return a.lane_lat[lane] + (b.lane_lat[lane] - a.lane_lat[lane]) * t;
  }

  /** Footpath strip at s: {lo, hi, centre, state, height_m} (t coordinates) or null where it is missing. */
  footpathAt(s) {
    const [a, b, t] = this._stationPair(s);
    const near = t < 0.5 ? a : b;
    if (!near.footpath) return null;
    const fa = a.footpath || near.footpath, fb = b.footpath || near.footpath;
    const lo = fa.lo + (fb.lo - fa.lo) * t, hi = fa.hi + (fb.hi - fa.hi) * t;
    return { lo, hi, centre: (lo + hi) / 2, state: near.footpath.state, height_m: near.footpath.height_m };
  }

  /** Which strip is under lateral position t at s: footpath | kerb | gap | <cross-section kind> | ground. */
  stripAt(s, lat) {
    const [, , t, i] = this._stationPair(s);
    const idx = t < 0.5 ? i : i + 1;
    const fp = this.footpathAt(s);
    if (fp && lat >= fp.lo && lat <= fp.hi) return 'footpath';
    const cs = this.cross[idx];
    const strips = cs ? cs.strips : [];
    const kerb = this.stations[idx] && this.stations[idx].kerb;
    if (kerb && Math.abs(lat - kerb.t) <= Math.max(kerb.width_m || 0.2, 0.3)) return 'kerb';
    for (const st of strips) if (lat >= st.t_lo && lat <= st.t_hi) return st.kind === 'carriageway' ? 'carriageway' : st.kind;
    if (!fp && this.footpathSide) {
      const road = strips.filter((x) => x.kind === 'carriageway');
      if (road.length) {
        const edge = this.footpathSide === 'left' ? Math.max(...road.map((x) => x.t_hi)) : Math.min(...road.map((x) => x.t_lo));
        if (this.footpathSide === 'left' ? lat >= edge : lat <= edge) return 'gap';
      }
    }
    return 'ground';
  }
}

export function makeManifestTrack(manifest) { return new ManifestTrack(manifest); }
