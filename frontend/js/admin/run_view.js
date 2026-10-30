// /admin/run.html?id=<run_id>: live pipeline view.
// Admin API paths used: GET /api/admin/status (login gate), GET /api/admin/runs/{run_id} (polled ~2 s while queued|running).

import { api, ApiError } from '/js/api.js?v=3';
import { gate, logout, el, fmtUsd, fmtUsdPrecise, fmtMs, setState, aget } from '/js/admin/auth.js?v=4';
import { renderReport, renderImagesUsed } from '/js/admin/report.js?v=4';

const POLL_MS = 2000;
const TERMINAL = new Set(['succeeded', 'failed', 'failed_budget']);
const state = { runId: new URLSearchParams(location.search).get('id'), run: null, error: null, polling: false, report: null };
window.__hero = { page: 'admin-run', get state() { return structuredClone(state); } };

const $ = (id) => document.getElementById(id);
const pretty = (obj) => JSON.stringify(obj, null, 2);

function chip(node, text) {
  node.textContent = text || '—';
  node.className = `chip ${text || ''}`;
}

function elapsedText(run) {
  if (!run.started_at) return 'not started';
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  return `elapsed ${fmtMs(end - new Date(run.started_at))}`;
}

const ADVICE = {
  budget_exhausted: 'Raise HERO_CLAUDE_BUDGET_USD / HERO_RUN_BUDGET_USD in .env and generate again.',
  upstream_error: 'Check the Google / Anthropic keys in .env and that the APIs are enabled, then generate again.',
  validation_error: 'The model output did not match the schema twice. Try generating again; if it persists, adjust the segment endpoints.',
};

function renderError(run) {
  const panel = $('error-panel');
  const e = run.error;
  if (!e || !TERMINAL.has(run.status) || run.status === 'succeeded') { panel.hidden = true; return; }
  panel.hidden = false;
  $('err-code').textContent = e.code || run.status;
  $('err-step').textContent = e.step || '?';
  $('err-message').textContent = e.message || 'The run failed without a message.';
  $('err-advice').textContent = ADVICE[e.code] || 'Inspect the failed step below, fix the cause, and generate again.';
}

function renderCost(run) {
  const c = run.cost || {};
  $('tokens').textContent = `${(c.input_tokens ?? 0).toLocaleString()} in · ${(c.output_tokens ?? 0).toLocaleString()} out`;
  $('cost-usd').textContent = fmtUsdPrecise(c.cost_usd);
  const g = run.google || {};
  $('google-counts').textContent = `${g.requests ?? 0} new / ${g.cache_hits ?? 0} cached`;
  const b = run.budget || {};
  $('budget-text').textContent = `${fmtUsd(b.spent_total_usd)} spent of ${fmtUsd(b.budget_usd)}`;
  const pct = b.budget_usd > 0 ? Math.min(100, (100 * (b.spent_total_usd || 0)) / b.budget_usd) : 0;
  $('budget-bar').style.width = `${pct}%`;
  const calls = c.calls || [];
  $('calls').querySelector('tbody').replaceChildren(...calls.map((k) => el('tr', {}, [
    el('td', { text: k.step }), el('td', { text: k.model }),
    el('td', { text: (k.input_tokens ?? 0).toLocaleString() }), el('td', { text: (k.output_tokens ?? 0).toLocaleString() }),
    el('td', { text: fmtUsdPrecise(k.cost_usd) }), el('td', { text: fmtMs(k.latency_ms) }),
    el('td', { text: k.ok ? 'yes' : 'no', class: k.ok ? '' : 'error' }),
  ])));
  $('calls').hidden = !calls.length;
  $('calls-empty').hidden = !!calls.length;
}

const ICON = { ok: '✓', failed: '✕', started: '…', skipped: '–', queued: '·' };

function renderSteps(run) {
  const steps = run.steps || [];
  $('steps-empty').hidden = !!steps.length;
  $('steps').replaceChildren(...steps.map((s) => el('li', { 'data-step': s.name, 'data-status': s.status }, [
    el('span', { class: 'icon', 'aria-label': s.status, text: ICON[s.status] || '·' }),
    el('span', { class: 'name', text: s.name }),
    el('span', { class: 'latency', text: s.status === 'started' ? 'running' : fmtMs(s.latency_ms) }),
    el('span', { class: 'detail', text: s.detail || '' }),
  ])));
}

const STREETVIEW_STEPS = new Set(['verify_streetview', 'fetch_streetview']);
const STREETVIEW_DONE = new Set(['ok', 'skipped', 'failed']);
// True only once the pipeline has actually checked Street View: the flag is false AND either the
// verify/fetch step has finished or the run is terminal. While queued/running before that step
// the backend may still report false/null, which is "not checked yet", not "unavailable".
function streetViewKnownUnavailable(run) {
  const im = run.imagery || {};
  if (im.streetview_available !== false) return false;
  if (TERMINAL.has(run.status)) return true;
  return (run.steps || []).some((s) => STREETVIEW_STEPS.has(s.name) && STREETVIEW_DONE.has(s.status));
}

let showAnnotated = false;
function renderImagery(run) {
  const im = run.imagery || {};
  const sat = im.satellite;
  const panos = im.streetview || [];
  $('imagery-empty').hidden = !!(sat || panos.length);
  $('streetview-notice').hidden = !streetViewKnownUnavailable(run);
  const fig = $('satellite-fig');
  if (sat && sat.url) {
    fig.hidden = false;
    const img = fig.querySelector('img');
    const annotated = sat.annotated_url || null;
    const src = showAnnotated && annotated ? annotated : sat.url;
    if (img.getAttribute('src') !== src) img.src = src;
    img.alt = showAnnotated && annotated ? 'Annotated satellite tile' : 'Satellite tile';
    $('satellite-caption').textContent = `Satellite · zoom ${sat.zoom ?? '?'} · ${sat.meters_per_px != null ? Number(sat.meters_per_px).toFixed(3) : '?'} m/px${im.osm_available === false ? ' · OSM unavailable' : ''}`;
    const toggle = $('annotated-toggle');
    toggle.hidden = !annotated;
    toggle.textContent = showAnnotated ? 'Show raw tile' : 'Show annotated';
  } else fig.hidden = true;
  const panoEls = panos.map((p, i) => el('figure', {}, [
    el('img', { src: p.url, alt: `Street View frame ${i + 1}`, loading: 'lazy' }),
    el('figcaption', { text: `Street View · x ${Number(p.x_m).toFixed(1)} m · heading ${Math.round(p.heading)}°` }),
  ]));
  const panosRoot = $('panos');
  if (panosRoot.childElementCount !== panoEls.length) panosRoot.replaceChildren(...panoEls);
}

function renderJson(detailsId, obj, emptyText) {
  const pre = $(detailsId).querySelector('pre');
  const text = obj ? pretty(obj) : emptyText;
  if (pre.textContent !== text) pre.textContent = text;
}

function renderIterations(run) {
  const its = run.iterations || [];
  $('iterations-empty').hidden = !!its.length;
  $('iterations').replaceChildren(...its.map((it) => {
    const ops = it.patch_ops || [];
    const verdict = it.accepted ? 'accepted' : 'needs changes';
    const applied = it.applied === false ? 'rejected' : it.applied === true ? 'applied' : '';
    return el('article', { class: 'iteration', 'data-n': it.n }, [
      el('figure', {}, it.preview_url
        ? [el('img', { class: 'preview', src: it.preview_url, alt: `Top-down preview, iteration ${it.n}` }), el('figcaption', { text: `Top-down preview · iteration ${it.n}` })]
        : [el('p', { class: 'muted', text: 'No preview rendered.' })]),
      el('div', {}, [
        el('h3', {}, [`Iteration ${it.n}`, el('span', { class: `chip ${it.accepted ? 'succeeded' : 'queued'}`, text: verdict }), applied ? el('span', { class: `chip ${applied === 'applied' ? 'ready' : 'failed'}`, text: applied }) : null]),
        el('p', { class: 'critique', text: it.critique || '(no critique)' }),
        ops.length
          ? el('div', { class: 'table-wrap' }, el('table', {}, [
              el('thead', {}, el('tr', {}, [el('th', { text: 'op' }), el('th', { text: 'target' }), el('th', { text: 'values' })])),
              el('tbody', {}, ops.map((op) => {
                const { op: name, object_id, ...rest } = op;
                const target = object_id || (rest.object && rest.object.id) || (name === 'set_coverage' ? `${rest.from_m}–${rest.to_m} m` : '—');
                return el('tr', {}, [el('td', {}, el('code', { text: name })), el('td', { text: String(target) }), el('td', {}, el('code', { text: JSON.stringify(rest) }))]);
              })),
            ]))
          : el('p', { class: 'muted', text: 'No patch ops.' }),
      ]),
    ]);
  }));
}

function renderFinal(run) {
  const scene = run.scene;
  $('final').hidden = !scene;
  $('final-empty').hidden = !!scene;
  if (!scene) return;
  const previewUrl = run.preview_url || (run.iterations || []).map((i) => i.preview_url).filter(Boolean).pop() || null;
  const fig = $('final-preview-fig');
  fig.hidden = !previewUrl;
  if (previewUrl && $('final-preview').getAttribute('src') !== previewUrl) $('final-preview').src = previewUrl;
  const w = scene.walkability || {};
  const g = scene.generation || {};
  $('walkability').replaceChildren(
    el('dt', { text: 'Walkability' }), el('dd', { text: `${w.score ?? '?'} / 100 · footpath ${w.footpath_coverage_pct ?? '?'}% · ${w.hazard_count ?? '?'} hazards · crossing ${w.crossing_present ? 'yes' : 'no'}` }),
    el('dt', { text: 'Quality' }), el('dd', { text: `${g.quality || '?'} · confidence ${g.confidence_overall ?? '?'} · ${g.review_iterations ?? 0} review iterations` }),
    el('dt', { text: 'Objects' }), el('dd', { text: `${(scene.objects || []).length} objects · ${(scene.tokens || []).length} tokens · ${(scene.buildings || []).length} buildings` }),
    ...(g.warnings && g.warnings.length ? [el('dt', { text: 'Warnings' }), el('dd', { text: g.warnings.join(' · ') })] : []),
  );
  renderJson('final-scene', scene, '');
}

function renderModeInfo(run) {
  const seg = run.segment || {};
  const mc = $('mode-chip');
  const ml = $('model-line');
  mc.hidden = !run.mode;
  if (run.mode) { mc.textContent = run.mode; mc.className = `chip ${run.mode}`; }
  const done = TERMINAL.has(run.status);
  if (!run.mode || !done) { ml.hidden = true; return; }
  ml.replaceChildren();
  const href = `/admin/segment.html?slug=${encodeURIComponent(seg.slug || '')}`;
  if (run.status !== 'succeeded') ml.append('Model unchanged — the run did not finish.');
  else if (run.mode === 'acquire') ml.append('Images fetched, not yet fused — ', el('a', { href, text: 'open the segment page' }), '.');
  else if (run.version_created) ml.append(el('a', { href, text: `Model v${run.base_version ?? '?'} → v${run.result_version}` }));
  else ml.append(`No change — model stays v${run.result_version ?? run.base_version ?? '?'}`, ' (', el('a', { href, text: 'segment page' }), ')');
  ml.hidden = false;
}

async function loadReport(run) {
  const card = $('report-card');
  if (!run.mode || !TERMINAL.has(run.status) || run.report_available === false) { card.hidden = true; return; }
  if (state.report || state.reportLoading) return;
  state.reportLoading = true;
  card.hidden = false;
  $('report-loading').hidden = false; $('report-error').hidden = true;
  try {
    const r = await aget(`/api/admin/runs/${encodeURIComponent(run.id || state.runId)}/report`);
    state.report = r;
    $('report-loading').hidden = true;
    renderReport($('report-body'), r, { slug: (run.segment || {}).slug });
  } catch (err) {
    $('report-loading').hidden = true;
    if (err.code === 'not_found') { card.hidden = true; } else {
      $('report-error').hidden = false;
      $('report-error').querySelector('.error').textContent = `Couldn't load the run report — ${err.message}`;
    }
  } finally { state.reportLoading = false; }
}

function renderPhase2Sections(run) {
  const used = (run.imagery && run.imagery.observations) || [];
  $('images-used').hidden = !used.length;
  if (used.length) renderImagesUsed($('images-used'), used);
  if (!run.mode) return;
  // Phase-1 sections with no data are hidden for Phase-2 runs, not shown empty.
  $('card-graphs').hidden = !run.layout && !run.obstacles;
  $('card-iter').hidden = !(run.iterations || []).length;
  $('card-final').hidden = !run.scene;
  const im = run.imagery || {};
  $('imagery').hidden = !((im.satellite && im.satellite.url) || (im.streetview || []).length);
}

function render(run) {
  state.run = run;
  const seg = run.segment || {};
  $('run-title').textContent = seg.name ? `${seg.name}` : 'Pipeline run';
  document.title = `#hero · ${seg.name || 'run'} · ${run.status}`;
  chip($('run-status'), run.status);
  $('elapsed').textContent = elapsedText(run);
  const live = !TERMINAL.has(run.status);
  $('live').hidden = !live;
  const play = $('play-link');
  if (seg.slug && (run.scene || (run.mode && run.version_created))) { play.href = `/play/?segment=${encodeURIComponent(seg.slug)}`; play.hidden = false; }
  renderError(run);
  renderCost(run);
  renderSteps(run);
  renderImagery(run);
  renderJson('layout-json', run.layout, 'Layout pass has not produced output yet.');
  renderJson('obstacles-json', run.obstacles, 'Obstacle pass has not produced output (skipped when Street View is unavailable).');
  renderIterations(run);
  renderFinal(run);
  renderModeInfo(run);
  renderPhase2Sections(run);
  loadReport(run);
  setState($('run'), 'populated');
  return live;
}

async function poll() {
  try {
    const run = await api.get(`/api/admin/runs/${encodeURIComponent(state.runId)}`, { admin: true });
    state.error = null;
    if (render(run)) setTimeout(poll, POLL_MS);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) { logout(); return; }
    state.error = err.code === 'not_found' ? `Run ${state.runId} was not found.` : `Couldn't load the run — ${err.message}`;
    if (state.run) {
      // keep showing the last good render, retry
      $('elapsed').textContent = `${state.error} Retrying…`;
      setTimeout(poll, POLL_MS * 2);
    } else {
      $('run-error').textContent = state.error;
      setState($('run'), 'error');
    }
  }
}

$('report-retry').addEventListener('click', () => { if (state.run) loadReport(state.run); });
$('annotated-toggle').addEventListener('click', () => { showAnnotated = !showAnnotated; if (state.run) renderImagery(state.run); });

gate({
  onReady: async () => {
    $('run').hidden = false;
    if (!state.runId) {
      $('run-error').textContent = 'No run id in the URL — open a run from the admin page.';
      setState($('run'), 'error');
      return;
    }
    setState($('run'), 'loading');
    poll();
  },
});
