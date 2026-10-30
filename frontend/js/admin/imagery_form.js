// Imagery section: observation grid with filters + "Add more imagery" form (density / headings / zoom / s range)
// with a live client-side estimate. Mirrors hero.imagery.plan.plan_images maths; cached = matching stored observations.
// API: GET .../observations (inventory), POST .../imagery (acquire-only), POST .../generate (fetch + generate).

import { el, apost, segPath, fmtUsd } from '/js/admin/auth.js?v=4';

export const EST_SV_USD = 0.007;
export const EST_SAT_USD = 0.002;
const HEADINGS = ['forward', 'back', 'left', 'right'];

function stationsFor(lo, span, spacing) {
  const out = [];
  for (let n = 0; n * spacing < span - 1e-9; n += 1) out.push(lo + n * spacing);
  out.push(lo + span);
  return out;
}

/** Pure estimate. observations: [{kind, view, station_s, zoom}] already stored for the segment. */
export function estimatePlan({ length_s, spacing, headings, zoom, range, maxImages = 60, satSpacing = 40, observations = [] }) {
  const lo = range ? Math.max(0, range.s_from) : 0;
  const hi = range ? Math.min(length_s, range.s_to) : length_s;
  if (!(hi > lo) || !headings.length) return { valid: false, planned: 0, cached: 0, fresh: 0, usd: 0, trimmed: false };
  const span = hi - lo;
  const nHead = headings.length;
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
  let cachedSv = 0;
  for (const s of stations) {
    for (const h of headings) {
      if (observations.some((o) => o.kind === 'streetview' && o.view === h && o.station_s != null && Math.abs(o.station_s - s) <= 0.6)) cachedSv += 1;
    }
  }
  const svTotal = stations.length * nHead;
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

export function createImageryForm({ form, slug, getLength, getObservations, limits, activeReason, onAuth }) {
  const q = (n) => form.querySelector(`[name=${n}]`);
  const spacing = q('spacing');
  const lim = limits || {};
  spacing.value = String(lim.sv_spacing_m || 10);
  const zoomDefault = [19, 20].includes(lim.sat_crop_zoom) ? lim.sat_crop_zoom : 20;
  form.querySelector(`[name=zoom][value="${zoomDefault}"]`).checked = true;
  const hdChecks = HEADINGS.map((h) => form.querySelector(`[name=heading][value=${h}]`));
  const defaults = new Set(lim.headings || HEADINGS);
  for (const c of hdChecks) c.checked = defaults.has(c.value);
  const msg = form.querySelector('.form-msg');
  const btnFetch = form.querySelector('[data-testid=fetch-only]');
  const btnGen = form.querySelector('[data-testid=fetch-generate]');

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
    const r = rangeValue();
    if (r) body.s_range = r;
    return body;
  }

  function refresh() {
    form.querySelector('.spacing-out').textContent = `${Number.parseFloat(spacing.value)} m`;
    const p = plan();
    const est = estimatePlan({
      length_s: getLength(), spacing: p.sv_spacing_m, headings: p.headings, zoom: p.sat_crop_zoom, range: p.s_range,
      maxImages: lim.max_images_per_run || 60, satSpacing: lim.sat_crop_spacing_m || 40, observations: getObservations(),
    });
    const out = form.querySelector('[data-testid=estimate]');
    const bad = !est.valid;
    out.textContent = bad
      ? (p.headings.length ? 'Check the stretch: "to" must be greater than "from".' : 'Pick at least one heading.')
      : `≈ ${est.fresh} new images ≈ ${fmtUsd(est.usd).replace('$', '$')} Google (${est.planned} planned, ${est.cached} already cached${est.trimmed ? '; spacing widened to fit the image cap' : ''})`;
    out.dataset.fresh = bad ? '' : String(est.fresh);
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
      const data = kind === 'imagery'
        ? await apost(segPath(slug, '/imagery'), body)
        : await apost(segPath(slug, '/generate'), { imagery: body });
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

  form.addEventListener('input', refresh);
  form.addEventListener('submit', (e) => e.preventDefault());
  btnFetch.addEventListener('click', () => submit('imagery'));
  btnGen.addEventListener('click', () => submit('generate'));
  refresh();
  return {
    refresh,
    setRange(a, b) {
      q('s_from').value = String(a); q('s_to').value = String(b); refresh();
      form.scrollIntoView({ block: 'center' });
    },
  };
}
