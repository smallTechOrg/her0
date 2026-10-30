// /admin/segment.html?slug=<slug>: what does the model know, how well, and from what.
// Admin API used: GET /api/admin/status (login + limits), GET /api/admin/segments, GET .../model, .../model/versions/{n},
// .../model/coverage, .../model/entities[/{id}], .../model/diff, .../observations, POST .../placement-preview,
// POST .../imagery, POST .../generate.

import { ApiError } from '/js/api.js?v=3';
import { gate, logout, el, aget, apost, segPath, setState } from '/js/admin/auth.js?v=5';
import { renderVersions } from '/js/admin/versions.js?v=4';
import { renderCoverage } from '/js/admin/coverage.js?v=4';
import { createEntities } from '/js/admin/entities.js?v=4';
import { renderDiff } from '/js/admin/diff.js?v=4';
import { renderObservations, createImageryForm } from '/js/admin/imagery_form.js?v=4';
import { placementButton, referenceLineText } from '/js/admin/placement.js?v=4';

const params = new URLSearchParams(location.search);
const slug = params.get('slug');
const state = { slug, status: null, segment: null, model: null, observations: null, diff: null, error: null };
window.__hero = { page: 'admin-segment', get state() { return structuredClone(state); } };

const $ = (id) => document.getElementById(id);
const sec = (id) => $(`sec-${id}`);

function authFailed(err) {
  if (err instanceof ApiError && err.status === 401) { logout(); return true; }
  return false;
}

/** Run `fn` for a section, driving its loading / error / populated(/empty) panels with an inline retry. */
async function load(section, fn) {
  setState(section, 'loading');
  const retry = section.querySelector('.retry');
  if (retry) retry.onclick = () => load(section, fn);
  try {
    const out = await fn();
    setState(section, out === 'empty' ? 'empty' : 'populated');
  } catch (err) {
    if (authFailed(err)) return;
    section.querySelector('[data-state=error] .error').textContent = `Couldn't load this section — ${err.message}`;
    setState(section, 'error');
  }
}

function activeReason() {
  const s = state.status;
  if (s && s.active_run_id) return 'A run is already in progress (one at a time).';
  if (s && s.remaining_usd <= 0) return 'Budget exhausted — raise HERO_CLAUDE_BUDGET_USD in .env.';
  return '';
}

function renderHeader() {
  const seg = state.segment;
  const model = state.model;
  $('seg-title').textContent = (seg && seg.name) || slug;
  document.title = `#hero · ${(seg && seg.name) || slug} · model`;
  $('seg-city').textContent = seg && seg.city ? seg.city : '';
  $('seg-length').textContent = seg && seg.length_m != null ? `${Number(seg.length_m).toFixed(0)} m` : '';
  const st = $('seg-status');
  if (seg) { st.textContent = seg.status; st.className = `chip ${seg.status}`; st.hidden = false; }
  const badge = $('model-badge');
  const warn = $('legacy-warning');
  warn.hidden = true;
  if (model && model.head_version != null) {
    const lvl = model.fidelity && model.fidelity.overall ? `L${model.fidelity.overall.level}` : 'L?';
    badge.textContent = `v${model.head_version} · ${lvl} · ${model.counts.observations} photos`;
    badge.dataset.fidelity = lvl;
    badge.hidden = false;
    if (model.legacy || model.placement_verified === false) {
      warn.textContent = model.legacy
        ? 'Legacy scene, placement unverified: this street came from the first-generation pipeline. Check placement, then Generate to rebuild it from photos.'
        : 'Placement unverified: the reference line may not lie on the road. Check placement before spending.';
      warn.hidden = false;
    } else if (lvl === 'L0') {
      warn.textContent = 'Map-only sketch: no street photos yet. Add imagery and Generate.';
      warn.hidden = false;
    }
  } else badge.hidden = true;
  const play = $('play-link');
  if (seg && seg.status === 'ready') { play.href = `/play/?segment=${encodeURIComponent(slug)}`; play.hidden = false; }
  const reason = activeReason();
  const gen = $('generate-btn');
  gen.disabled = !!reason; gen.title = reason || `Fuse the imagery plan into a new model version for ${slug}`;
}

async function onGenerate() {
  const btn = $('generate-btn');
  const msg = $('header-msg');
  btn.disabled = true; btn.textContent = 'Starting…'; msg.hidden = true;
  try {
    const data = await apost(segPath(slug, '/generate'));
    location.href = `/admin/run.html?id=${encodeURIComponent(data.run_id)}`;
  } catch (err) {
    if (authFailed(err)) return;
    msg.className = 'notice error';
    msg.textContent = err.code === 'run_in_progress' ? 'Another run is in progress — wait for it to finish.'
      : err.code === 'budget_exhausted' ? err.message : `Couldn't start the run — ${err.message}`;
    msg.hidden = false;
    btn.textContent = 'Generate';
    renderHeader();
  }
}

function renderReference() {
  const m = state.model;
  const ref = m && m.reference_line;
  const info = $('ref-info');
  if (!ref) {
    info.replaceChildren(el('p', { class: 'muted', text: 'No reference line yet — press Check placement to trace the road.' }));
  } else {
    const { ok, text } = referenceLineText(ref, m.placement_verified);
    info.replaceChildren(
      el('p', { class: ok ? 'placement-verdict ok' : 'placement-verdict error', 'data-testid': 'reference-source', text }),
      el('p', { class: 'muted', text: `${Number(ref.length_s).toFixed(1)} m long · ${(ref.points || []).length} points · source ${ref.source}` }),
    );
  }
  const box = $('placement-box');
  const action = $('placement-action');
  action.replaceChildren(placementButton(slug, { box, onError: authFailed }));
}

let entitiesCtl = null;
let formCtl = null;

async function loadAll() {
  $('page-error').hidden = true;
  let model;
  try {
    const [segs, mdl] = await Promise.all([aget('/api/admin/segments'), aget(segPath(slug, '/model'))]);
    state.segment = segs.find((s) => s.slug === slug) || null;
    model = state.model = mdl;
  } catch (err) {
    if (authFailed(err)) return;
    $('page-error-text').textContent = err.code === 'not_found' ? `Segment "${slug}" was not found.` : `Couldn't load this segment — ${err.message}`;
    $('page-error').hidden = false;
    $('page-retry').onclick = loadAll;
    return;
  }
  renderHeader();
  renderReference();
  const hasModel = model.head_version != null;
  $('no-model').hidden = hasModel;
  for (const id of ['versions', 'coverage', 'entities', 'diff']) sec(id).hidden = !hasModel;
  load(sec('imagery'), loadImagery);
  if (!hasModel) return;

  const head = model.head_version;
  let versionDetail = null;
  load(sec('versions'), async () => {
    const pre = renderVersions(sec('versions'), model.versions, { onCompare: compare });
    if (pre) compare(pre.from, pre.to);
    else setState(sec('diff'), 'empty');
  });
  load(sec('coverage'), async () => {
    const [cov, vd, ents, obs] = await Promise.all([
      aget(segPath(slug, `/model/coverage?version=${head}`)),
      aget(segPath(slug, `/model/versions/${head}`)),
      aget(segPath(slug, `/model/entities?version=${head}`)),
      aget(segPath(slug, '/observations?limit=200')),
    ]);
    versionDetail = vd;
    renderCoverage(sec('coverage').querySelector('.cov-body'), {
      coverage: cov, profile: (vd.model && vd.model.profile) || [], entities: ents,
      obsById: new Map(((obs && obs.items) || []).map((o) => [o.id, o])),
      onPickRange: (a, b) => formCtl && formCtl.setRange(a, b),
    });
  });
  load(sec('entities'), async () => {
    const list = await aget(segPath(slug, `/model/entities?version=${head}`));
    if (!entitiesCtl) entitiesCtl = createEntities({ root: sec('entities'), slug, getVersion: () => head, onAuth: authFailed });
    entitiesCtl.set(list);
    const want = params.get('entity');
    if (want && list.some((e) => e.id === want)) entitiesCtl.open(want);
    return list.length ? 'populated' : 'empty';
  });
}

async function loadImagery() {
  const data = await aget(segPath(slug, '/observations?limit=200'));
  state.observations = { total: data.total, loaded: data.items.length };
  renderObservations(sec('imagery'), data);
  const form = $('add-imagery');
  if (!formCtl) {
    formCtl = createImageryForm({
      form, slug, limits: state.status && state.status.limits,
      getLength: () => (state.model && state.model.reference_line && state.model.reference_line.length_s) || (state.segment && state.segment.length_m) || 100,
      getObservations: () => data.items,
      activeReason, onAuth: authFailed,
    });
    $('clear-range').onclick = () => { form.querySelector('[name=s_from]').value = ''; form.querySelector('[name=s_to]').value = ''; formCtl.refresh(); };
  } else formCtl.refresh();
}

async function compare(from, to) {
  const section = sec('diff');
  const run = async () => {
    const d = await aget(segPath(slug, `/model/diff?from=${from}&to=${to}`));
    state.diff = { from, to };
    renderDiff(section, d, { onOpenEntity: (id) => { entitiesCtl && entitiesCtl.open(id); } });
  };
  await load(section, run);
  const retry = section.querySelector('.retry');
  if (retry) retry.onclick = () => compare(from, to);
}

gate({
  onReady: async (status) => {
    state.status = status;
    if (!slug) {
      $('page').hidden = false;
      $('page-error-text').textContent = 'No segment in the URL — open a segment from the admin page.';
      $('page-error').hidden = false;
      $('page-retry').hidden = true;
      return;
    }
    $('page').hidden = false;
    $('generate-btn').addEventListener('click', onGenerate);
    await loadAll();
  },
});
