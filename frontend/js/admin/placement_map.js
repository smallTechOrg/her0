// Placement map for the upload form: the traced reference line drawn on a <canvas>, rotated so travel runs left -> right
// (left of travel is up), 10 m ticks, existing observations as dots (user photos blue), the selected row's marker in the
// accent colour (draggable, arrow keys = +-1 m). A click snaps to the nearest point on the line and reports (s, side).
// Below it the coverage cells again, so weak stretches can be clicked. No network: it only draws what it is given.

import { el } from '/js/admin/auth.js?v=5';

const LAT_M = 110574;
const PAD = 22;
const MIN_ACROSS_M = 14;
const CENTRE_BAND_M = 1.5;
const COLOURS = { L0: '#5b6270', L1: '#ffb100', L2: '#3ddc84', L3: '#4da3ff' };

/** Project reference-line points into (along, across) metres: along = travel direction, across > 0 = left of travel. */
export function projectLine(points) {
  if (!points || points.length < 2) return null;
  const lat0 = points[0].lat;
  const lon0 = points[0].lon;
  const kx = 111320 * Math.cos((lat0 * Math.PI) / 180);
  const xy = points.map((p) => ({ x: (p.lon - lon0) * kx, y: (p.lat - lat0) * LAT_M, s: p.s }));
  const last = xy[xy.length - 1];
  const bearing = Math.atan2(last.x, last.y); // clockwise from north
  const sb = Math.sin(bearing);
  const cb = Math.cos(bearing);
  const pts = xy.map((p) => ({ u: p.x * sb + p.y * cb, v: -p.x * cb + p.y * sb, s: p.s }));
  const deg = ((bearing * 180) / Math.PI + 360) % 360;
  return { pts, bearing_deg: deg };
}

/** Nearest point on the projected polyline to (u, v): {s, across, dist}. Pure, unit-testable. */
export function snapToLine(pts, u, v) {
  let best = null;
  for (let i = 0; i < pts.length - 1; i += 1) {
    const a = pts[i];
    const b = pts[i + 1];
    const du = b.u - a.u;
    const dv = b.v - a.v;
    const len2 = du * du + dv * dv || 1e-9;
    const t = Math.max(0, Math.min(1, ((u - a.u) * du + (v - a.v) * dv) / len2));
    const pu = a.u + t * du;
    const pv = a.v + t * dv;
    const d = Math.hypot(u - pu, v - pv);
    if (!best || d < best.dist) {
      // side is judged against the local line direction
      const cross = du * (v - pv) - dv * (u - pu);
      const sgn = cross >= 0 ? 1 : -1;
      best = { s: a.s + t * (b.s - a.s), across: sgn * d, dist: d };
    }
  }
  return best;
}

export const sideOf = (across) => (Math.abs(across) <= CENTRE_BAND_M ? 'centre' : across > 0 ? 'left' : 'right');

/** Compass word for the side of the road: bearing = travel direction (deg clockwise from north). */
export function compassSide(bearingDeg, side) {
  if (side === 'centre') return 'centre of the road';
  const b = (side === 'left' ? bearingDeg - 90 : bearingDeg + 90 + 360) % 360;
  const names = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];
  return `${names[Math.round(((b + 360) % 360) / 45) % 8]} side (${side} of travel)`;
}

/**
 * @param {HTMLElement} root  container (the [data-testid=upload-map] element)
 * @param {{onPick(s:number, side:string):void, onNudge(delta:number):void, onCell(s:number):void}} cb
 */
export function createPlacementMap(root, cb) {
  const canvas = el('canvas', { class: 'pm-canvas', tabindex: '0', role: 'img', 'data-testid': 'upload-map-canvas',
    'aria-label': 'Map of the street. Click where a photo was taken; with a marker focused, arrow keys move it by one metre.' });
  const empty = el('p', { class: 'muted', 'data-testid': 'upload-map-empty', hidden: true,
    text: 'No reference line yet — run Check placement first. You can still type each photo\'s position in metres.' });
  const hint = el('p', { class: 'muted small-print pm-hint', text: 'Click the map where the photo was taken (above the line = left of travel). Drag the highlighted marker or use ← → for ±1 m.' });
  const cells = el('div', { class: 'cov-strip pm-cells', 'data-testid': 'upload-map-cells', role: 'group', 'aria-label': 'Fidelity along the street; click a stretch to place the selected photo there' });
  root.replaceChildren(canvas, empty, hint, cells);

  let model = null; // {line, length, observations, bins}
  let markers = []; // [{id, s, side, selected}]
  let dragging = false;

  const ctx = canvas.getContext('2d');
  const geom = () => {
    const proj = model && projectLine(model.points);
    if (!proj) return null;
    const us = proj.pts.map((p) => p.u);
    const u0 = Math.min(...us);
    const u1 = Math.max(...us);
    const vmax = Math.max(MIN_ACROSS_M, ...proj.pts.map((p) => Math.abs(p.v)));
    const W = canvas.clientWidth || 300;
    const H = canvas.clientHeight || 200;
    const k = Math.min((W - 2 * PAD) / Math.max(u1 - u0, 1), (H - 2 * PAD) / (2 * vmax));
    const cx = W / 2;
    const cy = H / 2;
    const um = (u0 + u1) / 2;
    return {
      proj, W, H, k,
      toPx: (u, v) => [cx + (u - um) * k, cy - v * k],
      fromPx: (x, y) => [um + (x - cx) / k, (cy - y) / k],
    };
  }

  function pointAt(g, s) {
    const pts = g.proj.pts;
    for (let i = 0; i < pts.length - 1; i += 1) {
      if (s <= pts[i + 1].s || i === pts.length - 2) {
        const a = pts[i];
        const b = pts[i + 1];
        const t = Math.max(0, Math.min(1, (s - a.s) / ((b.s - a.s) || 1)));
        return [a.u + t * (b.u - a.u), a.v + t * (b.v - a.v)];
      }
    }
    return [0, 0];
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth || 300;
    const H = canvas.clientHeight || 200;
    if (canvas.width !== Math.round(W * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#12151b';
    ctx.fillRect(0, 0, W, H);
    const g = geom();
    empty.hidden = !!g;
    canvas.hidden = !g;
    if (!g) return;
    // road band (about 7 m either side of the line is "the road"; shown as a faint ribbon) and the line
    ctx.lineJoin = 'round';
    ctx.strokeStyle = 'rgba(255,255,255,.07)';
    ctx.lineWidth = Math.max(4, 14 * g.k);
    ctx.beginPath();
    g.proj.pts.forEach((p, i) => { const [x, y] = g.toPx(p.u, p.v); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.stroke();
    ctx.strokeStyle = '#00d4ff';
    ctx.lineWidth = 2.5;
    ctx.stroke();
    // 10 m ticks + labels every 20 m
    ctx.fillStyle = '#9aa3b2';
    ctx.font = '11px system-ui, sans-serif';
    ctx.strokeStyle = '#9aa3b2';
    ctx.lineWidth = 1;
    for (let s = 0; s <= model.length + 1e-6; s += 10) {
      const [u, v] = pointAt(g, s);
      const [x, y] = g.toPx(u, v);
      ctx.beginPath(); ctx.moveTo(x, y - 4); ctx.lineTo(x, y + 4); ctx.stroke();
      if (s % 20 === 0) ctx.fillText(`${s}`, x - 6, y + 17);
    }
    ctx.fillText('left of travel ↑', 6, 14);
    ctx.fillText('travel →', g.W - 58, g.H - 6);
    // existing observations
    for (const o of model.observations || []) {
      if (o.station_s == null) continue;
      const [u, v] = pointAt(g, o.station_s);
      const off = (o.offset_t != null ? o.offset_t : 0);
      const [x, y] = g.toPx(u, v + off);
      ctx.fillStyle = o.kind === 'user_photo' ? '#4da3ff' : 'rgba(200,200,200,.55)';
      ctx.beginPath(); ctx.arc(x, y, o.kind === 'user_photo' ? 4 : 2.2, 0, 2 * Math.PI); ctx.fill();
    }
    // row markers
    for (const m of markers) {
      if (m.s == null) continue;
      const [u, v] = pointAt(g, m.s);
      const off = m.side === 'left' ? 5.5 : m.side === 'right' ? -5.5 : 0;
      const [x, y] = g.toPx(u, v + off);
      ctx.fillStyle = m.selected ? '#ffb100' : '#e8eaee';
      ctx.strokeStyle = '#000';
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, m.selected ? 8 : 6, 0, 2 * Math.PI); ctx.fill(); ctx.stroke();
      if (m.label) { ctx.fillStyle = '#000'; ctx.font = 'bold 10px system-ui, sans-serif'; ctx.fillText(m.label, x - 3, y + 3.5); }
    }
  }

  function pick(ev) {
    const g = geom();
    if (!g) return;
    const r = canvas.getBoundingClientRect();
    const [u, v] = g.fromPx(ev.clientX - r.left, ev.clientY - r.top);
    const hit = snapToLine(g.proj.pts, u, v);
    if (!hit) return;
    cb.onPick(Math.round(hit.s * 10) / 10, sideOf(hit.across));
  }

  canvas.addEventListener('pointerdown', (ev) => { dragging = true; canvas.setPointerCapture && canvas.setPointerCapture(ev.pointerId); pick(ev); });
  canvas.addEventListener('pointermove', (ev) => { if (dragging) pick(ev); });
  const stop = () => { dragging = false; };
  canvas.addEventListener('pointerup', stop);
  canvas.addEventListener('pointercancel', stop);
  canvas.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') { ev.preventDefault(); cb.onNudge(ev.key === 'ArrowRight' ? 1 : -1); }
  });
  window.addEventListener('resize', draw);

  return {
    canvas,
    /** data: {points, length, observations, bins} */
    setData(data) {
      model = data;
      const len = data.length || 1;
      cells.replaceChildren(...(data.bins || []).map((b) => {
        const lvl = typeof b.level === 'string' ? b.level : `L${b.level}`;
        const c = el('button', { type: 'button', class: 'cov-cell', 'data-level': lvl, style: `width:${(100 * (b.s_to - b.s_from)) / len}%`,
          title: `${b.s_from}–${b.s_to} m · ${lvl}`, 'aria-label': `Place the selected photo at ${b.s_from} to ${b.s_to} metres (${lvl})` });
        c.addEventListener('click', () => cb.onCell(Math.round(((b.s_from + b.s_to) / 2) * 10) / 10));
        return c;
      }));
      draw();
    },
    setMarkers(list) { markers = list; draw(); },
    redraw: draw,
    bearing() { const p = model && projectLine(model.points); return p ? p.bearing_deg : null; },
    COLOURS,
  };
}
