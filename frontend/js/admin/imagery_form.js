// Imagery section: observation grid with filters + "Add more imagery" form (density / headings / zoom / s range)
// with a live client-side estimate. Mirrors hero.imagery.plan.plan_images maths; cached = matching stored observations.
// API: GET .../observations (inventory), POST .../imagery (acquire-only), POST .../generate (fetch + generate).

import { el, apost, segPath, fmtUsd } from '/js/admin/auth.js?v=5';
import { createQuality } from '/js/admin/quality_fields.js?v=1';

export const EST_SV_USD = 0.007;
export const EST_SAT_USD = 0.002;
const HEADINGS = ['forward', 'back', 'left', 'right', 'forward_left', 'forward_right', 'back_left', 'back_right'];
const HEADING_OFFSET = { forward: 0, forward_right: 45, right: 90, back_right: 135, back: 180, back_left: 225, left: 270, forward_left: 315 };
const PITCHES = [0, -30, 25];
const FOVS = [90, 50];
const PANO_SNAP_M = 6; // as the server estimate: a station snaps to the nearest stored panorama

function stationsFor(lo, span, spacing) {
  const out = [];
  for (let n = 0; n * spacing < span - 1e-9; n += 1) out.push(lo + n * spacing);
  out.push(lo + span);
  return out;
}

const quant = (h) => ((Math.round(h / 5) * 5) % 360 + 360) % 360;

/**
 * Pure estimate. observations: [{kind, view, station_s, zoom, pano_id, heading_deg, pitch_deg, fov_deg}] already stored.
 * Every station is shot at headings x pitches x fovs; stations that land on the same stored panorama are one image.
 */
export function estimatePlan({ length_s, spacing, headings, zoom, range, maxImages = 60, satSpacing = 40, observations = [],
  pitches = [0], fovs = [90] }) {
  const lo = range ? Math.max(0, range.s_from) : 0;
  const hi = range ? Math.min(length_s, range.s_to) : length_s;
  if (!(hi > lo) || !headings.length) return { valid: false, planned: 0, cached: 0, fresh: 0, usd: 0, trimmed: false };
  const span = hi - lo;
  const nHead = headings.length * pitches.length * fovs.length;
  let nCrops = Math.max(1, Math.ceil(span / satSpacing - 1e-9));
  const budgetNonSv = Math.max(0, maxImages - 2 * nHead);
  if (nCrops + 1 > budgetNonSv) nCrops = Math.max(0, budgetNonSv - 1);
  let stations = stationsFor(lo, span, spacing);
  const nMax = Math.floor((maxImages - nCrops - 1) / nHead);
  const trimmed = stations.length > nMax;
  if (trimmed) {
    const n = Math.max(1, nMax);
    stations = n >= 2 ? Array.from({ length: n }, (_, i) => lo + (i * span) / (n - 1)) : [lo + span / 2];
  }
  const sv = observations.filter((o) => o.kind === 'streetview' && o.station_s != null);
  const have = new Set(sv.filter((o) => o.pano_id).map((o) => `${o.pano_id}|${quant(o.heading_deg || 0)}|${o.pitch_deg || 0}|${o.fov_deg || 90}`));
  const seen = new Set();
  let cachedSv = 0;
  for (const s of stations) {
    let near = null;
    for (const o of sv) if (o.pano_id && (!near || Math.abs(o.station_s - s) < Math.abs(near.station_s - s))) near = o;
    const pano = near && Math.abs(near.station_s - s) <= PANO_SNAP_M ? near.pano_id : `s${s.toFixed(1)}`;
    // relative heading: stored headings are compass, so offset from the nearest stored forward frame of that pano
    const fwd = sv.find((o) => o.pano_id === pano && o.view === 'forward');
    for (const f of fovs) {
      for (const p of pitches) {
        for (const h of headings) {
          const head = fwd ? quant((fwd.heading_deg || 0) + HEADING_OFFSET[h]) : h;
          const key = `${pano}|${head}|${p}|${f}`;
          if (seen.has(key)) continue;
          seen.add(key);
          // or, as before tilts / zoom existed: a level, wide frame of this view stored at this station
          if (have.has(key) || (p === 0 && f === 90 && observations.some((o) => o.kind === 'streetview' && o.view === h
            && o.station_s != null && Math.abs(o.station_s - s) <= 0.6 && !(o.pitch_deg || 0)))) cachedSv += 1;
        }
      }
    }
  }
  const svTotal = seen.size;
  const crops = observations.filter((o) => o.kind === 'satellite_crop' && o.zoom === zoom).length;
  const cachedCrops = Math.min(crops, nCrops);
  const cachedOverview = observations.some((o) => o.kind === 'satellite_overview') ? 1 : 0;
  const planned = svTotal + nCrops + 1;
  const cached = cachedSv + cachedCrops + cachedOverview;
  const usd = (svTotal - cachedSv) * EST_SV_USD + (nCrops - cachedCrops + 1 - cachedOverview) * EST_SAT_USD;
  return { valid: true, planned, cached, fresh: planned - cached, usd, trimmed };
}

export function renderObservations(root, data, { onAuth } = {}) {
  const items = data.items || [];
  const grid = root.querySelector('.obs-grid');
  const fKind = root.querySelector('[name=o-kind]');
  const fView = root.querySelector('[name=o-view]');
  const counts = root.querySelector('.obs-counts');
  const draw = () => {
    const rows = items.filter((o) => (!fKind.value || o.kind === fKind.value) && (!fView.value || o.view === fView.value));
    const fused = rows.filter((o) => o.fused_in_version != null).length;
    counts.textContent = `${rows.length} shown of ${data.total} total${items.length < data.total ? ` (latest ${items.length} loaded)` : ''} · ${fused} fused · ${rows.length - fused} not yet fused`;
    grid.replaceChildren(...rows.map((o) => el('figure', { class: 'obs', 'data-obs-id': o.id }, [
      el('a', { href: o.url, target: '_blank', rel: 'noopener' }, el('img', { src: o.url, alt: `${o.kind} ${o.view || ''}`, loading: 'lazy' })),
      el('figcaption', {}, [
        `${o.view || o.kind.replace('satellite_', 'sat ')}${o.station_s != null ? ` · ${Number(o.station_s).toFixed(0)} m` : ''}${o.capture_date ? ` · ${o.capture_date}` : ''} `,
        el('span', { class: `chip ${o.fused_in_version != null ? 'ready' : 'queued'}`, text: o.fused_in_version != null ? `fused in v${o.fused_in_version}` : 'not yet fused' }),
      ]),
    ])));
    root.querySelector('[data-role=empty-obs]').hidden = !!rows.length;
  };
  const kinds = [...new Set(items.map((o) => o.kind))].sort();
  fKind.replaceChildren(el('option', { value: '', text: 'All kinds' }), ...kinds.map((k) => el('option', { value: k, text: k })));
  fKind.oninput = draw; fView.oninput = draw;
  draw();
}

export function createImageryForm({ form, slug, getLength, getObservations, limits, activeReason, onAuth, getModel, models }) {
  const q = (n) => form.querySelector(`[name=${n}]`);
  const spacing = q('spacing');
  const lim = limits || {};
  spacing.value = String(lim.sv_spacing_m || 10);
  const zoomDefault = [19, 20, 21].includes(lim.sat_crop_zoom) ? lim.sat_crop_zoom : 20;
  form.querySelector(`[name=zoom][value="${zoomDefault}"]`).checked = true;
  const hdChecks = HEADINGS.map((h) => form.querySelector(`[name=heading][value=${h}]`));
  const defaults = new Set(lim.headings || HEADINGS);
  for (const c of hdChecks) c.checked = defaults.has(c.value);
  const pitchChecks = PITCHES.map((v) => form.querySelector(`[name=pitch][value="${v}"]`));
  const fovChecks = FOVS.map((v) => form.querySelector(`[name=fov][value="${v}"]`));
  for (const c of pitchChecks) c.checked = c.value === '0';
  for (const c of fovChecks) c.checked = c.value === '90';
  const msg = form.querySelector('.form-msg');
  const btnFetch = form.querySelector('[data-testid=fetch-only]');
  const btnGen = form.querySelector('[data-testid=fetch-generate]');

  const maxFields = form.querySelector('[data-testid=max-fields]');
  const extraZoom = q('extra_zoom');
  const maxImages = q('max_images');
  const framesOut = form.querySelector('[data-testid=frames-per-station]');
  const claudeOut = form.querySelector('[data-testid=estimate-claude]');
  const quality = models ? createQuality({
    container: form.querySelector('.quality-slot'), models, testid: 'imagery',
    // the preset fills the editable fields of this form (the server owns the values)
    onPreset(preset, on) {
      maxFields.hidden = !on;
      if (on) {
        spacing.value = String(preset.sv_spacing_m);
        const have = new Set(preset.headings);
        for (const c of hdChecks) c.checked = have.has(c.value);
        const ps = new Set((preset.pitches_deg || [0]).map(String));
        for (const c of pitchChecks) c.checked = ps.has(c.value);
        const fs = new Set((preset.fovs_deg || [90]).map(String));
        for (const c of fovChecks) c.checked = fs.has(c.value);
        const z = form.querySelector(`[name=zoom][value="${preset.sat_crop_zoom}"]`);
        if (z) z.checked = true;
        extraZoom.value = preset.sat_extra_zoom == null ? '' : String(preset.sat_extra_zoom);
        maxImages.value = String(preset.max_images);
      }
      refresh();
    },
    onChange: () => refresh(),
  }) : null;

  function rangeValue() {
    const a = Number.parseFloat(q('s_from').value);
    const b = Number.parseFloat(q('s_to').value);
    if (Number.isFinite(a) && Number.isFinite(b)) return { s_from: a, s_to: b };
    return null;
  }

  function plan() {
    const body = {
      sv_spacing_m: Number.parseFloat(spacing.value),
      headings: hdChecks.filter((c) => c.checked).map((c) => c.value),
      sat_crop_zoom: Number(form.querySelector('[name=zoom]:checked').value),
    };
    const pitches = pitchChecks.filter((c) => c.checked).map((c) => Number(c.value));
    const fovs = fovChecks.filter((c) => c.checked).map((c) => Number(c.value));
    // only sent when they differ from the defaults (level, wide): the request is then exactly as before
    if (pitches.length && !(pitches.length === 1 && pitches[0] === 0)) body.pitches_deg = pitches;
    if (fovs.length && !(fovs.length === 1 && fovs[0] === 90)) body.fovs_deg = fovs;
    const r = rangeValue();
    if (r) body.s_range = r;
    if (quality && quality.on()) { // max-quality extras are sent only while the preset is on
      const mi = Number.parseInt(maxImages.value, 10);
      if (Number.isFinite(mi)) body.max_images = mi;
      if (extraZoom.value) body.sat_extra_zoom = Number(extraZoom.value);
      if (quality.preset && quality.preset.sat_crop_spacing_m) body.sat_crop_spacing_m = quality.preset.sat_crop_spacing_m;
    }
    return body;
  }

  let estTimer = null;
  let estSeq = 0;
  /** Server-side estimate (authoritative: it prices passes, review rounds and the overlapping tile set). */
  function serverEstimate(p) {
    clearTimeout(estTimer);
    estTimer = setTimeout(async () => {
      const seq = ++estSeq;
      const req = { imagery: p };
      const q = quality && quality.value();
      if (q) req.quality = q;
      if (getModel && getModel()) req.model = getModel();
      try {
        const d = await apost(segPath(slug, '/imagery/estimate'), req);
        if (seq !== estSeq) return;
        const e = d.est;
        claudeOut.textContent = `≈ ${d.new} new of ${d.planned} planned images · ≈ ${fmtUsd(e.google_usd)} Google + ≈ ${fmtUsd(e.claude_usd)} Claude `
          + `(upper bound: ${d.extraction_passes} extraction pass${d.extraction_passes === 1 ? '' : 'es'}, ${d.review_rounds} review round${d.review_rounds === 1 ? '' : 's'}) `
          + `= ≈ ${fmtUsd(e.total_usd)} total · Model: ${e.model}`;
        claudeOut.dataset.total = String(e.total_usd);
        claudeOut.dataset.claude = String(e.claude_usd);
      } catch (err) {
        if (seq !== estSeq) return;
        claudeOut.textContent = '';
        delete claudeOut.dataset.total;
      }
    }, 150);
  }

  function refresh() {
    form.querySelector('.spacing-out').textContent = `${Number.parseFloat(spacing.value)} m`;
    const p = plan();
    const est = estimatePlan({
      length_s: getLength(), spacing: p.sv_spacing_m, headings: p.headings, zoom: p.sat_crop_zoom, range: p.s_range,
      maxImages: (quality && quality.on() && p.max_images) || lim.max_images_per_run || 60,
      satSpacing: (quality && quality.on() && p.sat_crop_spacing_m) || lim.sat_crop_spacing_m || 40, observations: getObservations(),
      pitches: p.pitches_deg || [0], fovs: p.fovs_deg || [90],
    });
    const out = form.querySelector('[data-testid=estimate]');
    const noAngles = !pitchChecks.some((c) => c.checked) || !fovChecks.some((c) => c.checked);
    const bad = !est.valid || noAngles;
    out.textContent = bad
      ? (noAngles ? 'Pick at least one tilt and one field of view.'
        : p.headings.length ? 'Check the stretch: "to" must be greater than "from".' : 'Pick at least one heading.')
      : `≈ ${est.fresh} new images ≈ ${fmtUsd(est.usd).replace('$', '$')} Google (${est.planned} planned, ${est.cached} already cached${est.trimmed ? '; spacing widened to fit the image cap' : ''})`;
    out.dataset.fresh = bad ? '' : String(est.fresh);
    framesOut.textContent = String(p.headings.length * (p.pitches_deg || [0]).length * (p.fovs_deg || [90]).length);
    if (bad) { claudeOut.textContent = ''; } else serverEstimate(p);
    const reason = activeReason();
    const off = bad || !!reason;
    for (const b of [btnFetch, btnGen]) { b.disabled = off; b.title = reason || (bad ? 'Fix the plan first' : ''); }
    form.querySelector('.active-note').textContent = reason || '';
    form.querySelector('.active-note').hidden = !reason;
  }

  async function submit(kind) {
    msg.hidden = true;
    for (const b of [btnFetch, btnGen]) b.disabled = true;
    try {
      const body = plan();
      const req = { imagery: body };
      if (getModel && getModel()) req.model = getModel();
      const q = quality && quality.value();
      if (q) req.quality = q;
      const data = kind === 'imagery'
        ? await apost(segPath(slug, '/imagery'), body)
        : await apost(segPath(slug, '/generate'), req);
      location.href = `/admin/run.html?id=${encodeURIComponent(data.run_id)}`;
    } catch (err) {
      if (onAuth && onAuth(err)) return;
      msg.className = 'form-msg notice error';
      msg.textContent = err.code === 'run_in_progress' ? 'Another run is in progress — wait for it to finish.'
        : err.code === 'budget_exhausted' ? err.message : `Couldn't start the run — ${err.message}`;
      msg.hidden = false;
      refresh();
    }
  }

  if (models) models.selects.add(() => { if (form.isConnected) refresh(); }); // a model switch elsewhere re-prices the estimate
  form.addEventListener('input', refresh);
  form.addEventListener('submit', (e) => e.preventDefault());
  btnFetch.addEventListener('click', () => submit('imagery'));
  btnGen.addEventListener('click', () => submit('generate'));
  refresh();
  return {
    refresh, quality,
    setRange(a, b) {
      q('s_from').value = String(a); q('s_to').value = String(b); refresh();
      form.scrollIntoView({ block: 'center' });
    },
  };
}
