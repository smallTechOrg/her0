// /admin/ dashboard: status bar, segment table + Generate, create form, recent runs.
// Admin API paths used: GET /api/admin/status, GET /api/admin/segments, POST /api/admin/segments,
// POST /api/admin/segments/{slug}/generate, GET /api/admin/runs?limit=20.

import { api, ApiError } from '/js/api.js?v=3';
import { gate, logout, el, fmtUsd, fmtMs, setState } from '/js/admin/auth.js?v=5';
import { placementButton } from '/js/admin/placement.js?v=4';

const state = { status: null, segments: null, runs: null, error: null };
window.__hero = { page: 'admin', get state() { return structuredClone(state); } };

const $ = (id) => document.getElementById(id);

function chip(text) {
  return el('span', { class: `chip ${text || ''}`, text: text || '—' });
}

function renderStatus(s) {
  state.status = s;
  $('spend').textContent = `${fmtUsd(s.spent_usd)} / ${fmtUsd(s.budget_usd)}`;
  $('remaining').textContent = `${fmtUsd(s.remaining_usd)} remaining · per-run cap ${fmtUsd(s.run_budget_usd)}`;
  const pct = s.budget_usd > 0 ? Math.min(100, (100 * s.spent_usd) / s.budget_usd) : 0;
  const bar = $('spend-bar');
  bar.style.width = `${pct}%`;
  bar.classList.toggle('over', s.remaining_usd <= 0);
  bar.parentElement.setAttribute('aria-valuenow', String(Math.round(pct)));
  const r = s.runs || {};
  $('runs-totals').textContent = `${r.total ?? 0} total · ${r.succeeded ?? 0} succeeded · ${r.failed ?? 0} failed · ${r.failed_budget ?? 0} failed on budget`;
  const g = s.google || {};
  $('google-totals').textContent = `${g.requests ?? 0} Google requests · ${g.cache_hits ?? 0} cache hits · est. ${fmtUsd(g.est_cost_usd)}`;
  const m = s.models || {};
  $('models').textContent = `vision ${m.vision || '?'} · review ${m.review || '?'}`;
  const sv = s.streetview_available_last;
  $('streetview-last').textContent = sv === null || sv === undefined ? 'not checked yet' : sv ? 'available' : 'unavailable';
}

async function refreshStatus() {
  try { renderStatus(await api.get('/api/admin/status', { admin: true })); } catch (err) { handleAuthError(err); }
}

function handleAuthError(err) {
  if (err instanceof ApiError && err.status === 401) { logout(); return true; }
  return false;
}

function generateDisabledReason(seg) {
  const s = state.status;
  const anyRunning = (state.segments || []).some((x) => x.latest_run && ['queued', 'running'].includes(x.latest_run.status));
  if (anyRunning) return 'A run is already in progress (one at a time).';
  if (s && s.remaining_usd <= 0) return `Budget exhausted (${fmtUsd(s.spent_usd)} of ${fmtUsd(s.budget_usd)} spent).`;
  if (seg.status === 'generating') return 'This segment is generating.';
  return '';
}

function modelCell(m) {
  if (!m) return el('span', { class: 'muted', text: '—' });
  const bits = [el('span', { class: 'model-cell', 'data-fidelity': m.fidelity_level, text: `v${m.head_version} · ${m.fidelity_level}` })];
  if (m.legacy) bits.push(' ', el('span', { class: 'chip kind-legacy', title: 'Imported Phase-1 scene; placement unverified', text: 'legacy · placement unverified' }));
  return el('span', {}, bits);
}

function segmentRow(seg) {
  const lr = seg.latest_run;
  const reason = generateDisabledReason(seg);
  const btn = el('button', {
    type: 'button', text: lr && lr.status === 'succeeded' ? 'Generate again' : 'Generate',
    disabled: !!reason, title: reason || `Start a pipeline run for ${seg.name}`,
    onclick: () => generate(seg, btn),
  });
  const latest = lr
    ? el('span', {}, [
        chip(lr.status), ' ',
        el('a', { href: `/admin/run.html?id=${encodeURIComponent(lr.id)}`, text: `${fmtUsd(lr.cost_usd)} · view` }),
      ])
    : el('span', { class: 'muted', text: 'No runs yet' });
  return el('tr', { 'data-slug': seg.slug }, [
    el('td', {}, [seg.name, el('span', { class: 'muted', text: seg.slug })]),
    el('td', { text: seg.city || '' }),
    el('td', { text: seg.length_m != null ? `${Number(seg.length_m).toFixed(0)} m` : '—' }),
    el('td', {}, chip(seg.status)),
    el('td', { text: seg.quality || '—' }),
    el('td', {}, modelCell(seg.model)),
    el('td', {}, latest),
    el('td', { class: 'row-actions' }, [
      el('a', { class: 'button-link small', href: `/admin/segment.html?slug=${encodeURIComponent(seg.slug)}`, 'data-testid': 'open-segment', text: 'Open' }), ' ', btn]),
  ]);
}

function renderSegments() {
  const root = $('segments');
  if (state.error) { $('segments-error').textContent = state.error; setState(root, 'error'); return; }
  if (!state.segments) { setState(root, 'loading'); return; }
  if (!state.segments.length) { setState(root, 'empty'); return; }
  const body = $('segments-body');
  body.replaceChildren(...state.segments.map(segmentRow));
  setState(root, 'populated');
}

async function loadSegments() {
  state.error = null;
  if (!state.segments) renderSegments();
  try {
    state.segments = await api.get('/api/admin/segments', { admin: true });
  } catch (err) {
    if (handleAuthError(err)) return;
    state.error = `Couldn't load segments — ${err.message}`;
  }
  renderSegments();
}

function showMsg(text, kind = '') {
  const m = $('segments-msg');
  m.textContent = text;
  m.className = `notice ${kind}`;
  m.hidden = !text;
}

async function generate(seg, btn) {
  btn.disabled = true;
  btn.textContent = 'Starting…';
  showMsg(`Starting a run for ${seg.name}…`);
  try {
    const data = await api.post(`/api/admin/segments/${encodeURIComponent(seg.slug)}/generate`, undefined, { admin: true });
    location.href = `/admin/run.html?id=${encodeURIComponent(data.run_id)}`;
  } catch (err) {
    if (handleAuthError(err)) return;
    const why = err.code === 'run_in_progress' ? 'Another run is in progress — wait for it to finish.'
      : err.code === 'budget_exhausted' ? err.message
      : `Couldn't start the run — ${err.message}`;
    showMsg(why, 'error');
    btn.disabled = false;
    btn.textContent = 'Generate';
    await Promise.all([refreshStatus(), loadSegments()]);
  }
}

function renderRuns() {
  const root = $('runs');
  if (state.runs === null) { setState(root, 'loading'); return; }
  if (state.runs === false) { setState(root, 'error'); return; }
  if (!state.runs.length) { setState(root, 'empty'); return; }
  $('runs-body').replaceChildren(...state.runs.map((r) => {
    const seg = r.segment || {};
    const started = r.started_at ? new Date(r.started_at).toLocaleString() : '—';
    const dur = r.started_at && r.finished_at ? fmtMs(new Date(r.finished_at) - new Date(r.started_at)) : (r.status === 'running' ? 'running' : '—');
    return el('tr', {}, [
      el('td', { text: started }),
      el('td', { text: seg.name || r.segment_slug || seg.slug || '—' }),
      el('td', {}, r.mode ? chip(r.mode) : el('span', { class: 'muted', text: '—' })),
      el('td', {}, chip(r.status)),
      el('td', { text: r.version_created === true ? 'new version' : r.version_created === false ? 'no change' : '—' }),
      el('td', { text: fmtUsd(r.cost_usd) }),
      el('td', { text: dur }),
      el('td', {}, el('a', { href: `/admin/run.html?id=${encodeURIComponent(r.id)}`, text: 'Open' })),
    ]);
  }));
  setState(root, 'populated');
}

async function loadRuns() {
  try {
    state.runs = await api.get('/api/admin/runs?limit=20', { admin: true });
  } catch (err) {
    if (handleAuthError(err)) return;
    $('runs-error').textContent = `Couldn't load runs — ${err.message}`;
    state.runs = false;
  }
  renderRuns();
}

const pick = { candidates: [], selected: null };

function mapsLink(c) { return `https://www.google.com/maps/search/?api=1&query=${c.lat},${c.lon}`; }

function renderCandidates() {
  const list = $('resolve-list');
  list.replaceChildren(...pick.candidates.map((c, i) => {
    const radio = el('input', { type: 'radio', name: 'cand', value: String(i) });
    radio.checked = i === pick.selected;
    radio.addEventListener('change', () => selectCandidate(i));
    const road = c.road_name ? ` · road: ${c.road_name}` : '';
    const dir = c.heading_deg == null ? 'road direction not found: enter it below'
      : `road runs ${c.travel_compass} (${c.heading_deg}°)`;
    const li = el('li', { 'data-selected': String(i === pick.selected) }, [
      radio,
      el('label', {}, [
        el('b', { text: c.label }),
        el('small', { text: `${c.lat}, ${c.lon} · ${c.provider}${road} · ${dir} · ` }, [
          el('a', { href: mapsLink(c), target: '_blank', rel: 'noopener', text: 'check on map' })]),
      ]),
    ]);
    li.addEventListener('click', (ev) => { if (ev.target.tagName !== 'A') { radio.checked = true; selectCandidate(i); } });
    return li;
  }));
}

function selectCandidate(i) {
  pick.selected = i;
  const c = pick.candidates[i];
  const side = $('f-side');
  const keep = side.value || 'left';
  side.replaceChildren(
    el('option', { value: 'left', text: c.left_side ? `${c.left_side} side (left of travel)` : 'left' }),
    el('option', { value: 'right', text: c.right_side ? `${c.right_side} side (right of travel)` : 'right' }),
  );
  side.value = keep;
  $('f-heading').value = c.heading_deg == null ? '' : String(c.heading_deg);
  renderCandidates();
  updatePreview();
}

function updatePreview() {
  const c = pick.candidates[pick.selected];
  const half = Number.parseFloat($('f-half').value);
  const ok = !!c && Number.isFinite(half) && half >= 50 && half <= 100 && Number.isFinite(Number.parseFloat($('f-heading').value));
  $('segment-preview').textContent = ok ? `Segment: ${half} m either way of this point, ${2 * half} m long.` : '';
  $('create-btn').disabled = !ok;
}

async function onFind() {
  const msg = $('create-msg');
  const btn = $('find-btn');
  const address = $('f-address').value.trim();
  const city = $('f-city').value.trim();
  if (!address || !city) { msg.className = 'wide error'; msg.textContent = 'Enter an address and a city.'; msg.hidden = false; return; }
  btn.disabled = true; btn.textContent = 'Finding…'; msg.hidden = true;
  try {
    const data = await api.post('/api/admin/segments/resolve', { address, city }, { admin: true });
    pick.candidates = data.candidates;
    $('resolve-results').hidden = false;
    selectCandidate(0);
  } catch (err) {
    if (handleAuthError(err)) return;
    pick.candidates = []; pick.selected = null;
    $('resolve-results').hidden = true;
    $('create-btn').disabled = true;
    msg.className = 'wide error'; msg.textContent = err.message; msg.hidden = false;
  } finally {
    btn.disabled = false; btn.textContent = 'Find location';
  }
}

async function onCreate(ev) {
  ev.preventDefault();
  const msg = $('create-msg');
  const btn = $('create-btn');
  const c = pick.candidates[pick.selected];
  if (!c) { msg.className = 'wide error'; msg.textContent = 'Press "Find location" and pick a place first.'; msg.hidden = false; return; }
  const body = {
    name: $('f-address').value.trim(), city: $('f-city').value.trim(), lat: c.lat, lon: c.lon,
    heading_deg: Number.parseFloat($('f-heading').value), half_length_m: Number.parseFloat($('f-half').value),
    footpath_side: $('f-side').value,
  };
  btn.disabled = true; btn.textContent = 'Creating…';
  msg.hidden = true;
  try {
    const seg = await api.post('/api/admin/segments/from-address', body, { admin: true });
    msg.className = 'wide notice ok';
    msg.textContent = `Created "${seg.name}" (${seg.slug}, ${Number(seg.length_m).toFixed(0)} m). Check the placement, then press Generate to build it.`;
    msg.hidden = false;
    const box = $('placement-box');
    box.hidden = true;
    const acts = $('created-actions');
    acts.replaceChildren(placementButton(seg.slug, { box, onError: handleAuthError }),
      el('a', { class: 'button-link', href: `/admin/segment.html?slug=${encodeURIComponent(seg.slug)}`, text: 'Open segment page' }));
    acts.hidden = false;
    await loadSegments();
    document.querySelector(`tr[data-slug="${CSS.escape(seg.slug)}"]`)?.scrollIntoView({ block: 'center' });
  } catch (err) {
    if (handleAuthError(err)) return;
    msg.className = 'wide error';
    msg.textContent = err.code === 'validation_error' ? `Not created: ${err.message}` : `Couldn't create the segment — ${err.message}`;
    msg.hidden = false;
  } finally {
    btn.textContent = 'Create segment';
    updatePreview();
  }
}

let timer = null;
function schedulePoll() {
  clearInterval(timer);
  timer = setInterval(async () => {
    const anyRunning = (state.segments || []).some((x) => x.latest_run && ['queued', 'running'].includes(x.latest_run.status));
    if (anyRunning) await Promise.all([refreshStatus(), loadSegments(), loadRuns()]);
  }, 3000);
}

gate({
  onReady: async (status) => {
    renderStatus(status);
    $('dashboard').hidden = false;
    $('logout').hidden = false;
    $('logout').addEventListener('click', logout);
    $('refresh').addEventListener('click', () => Promise.all([refreshStatus(), loadSegments(), loadRuns()]));
    $('segments-retry').addEventListener('click', loadSegments);
    $('create-form').addEventListener('submit', onCreate);
    $('find-btn').addEventListener('click', onFind);
    $('f-address').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); onFind(); } });
    for (const id of ['f-half', 'f-heading']) $(id).addEventListener('input', updatePreview);
    await Promise.all([loadSegments(), loadRuns()]);
    schedulePoll();
  },
});
