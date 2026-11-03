// "How to improve this model" panel: weakest intervals with hints, four one-click action cards (Fill gaps preview,
// Add more imagery [form lives in imagery_form.js], Upload photos [upload_form.js], Regenerate with what we have) and the
// numbered how-to steps. API: GET .../improve-plan, POST .../fill-gaps/preview, POST .../fill-gaps, POST .../regenerate,
// GET /api/admin/models. Nothing here spends money without an explicit button press.

import { el, aget, apost, segPath, fmtUsd } from '/js/admin/auth.js?v=5';
import { renderModelSelect } from '/js/admin/model_select.js';
import { createQuality } from '/js/admin/quality_fields.js?v=1';

const LEVEL_MAX = 3;
const levelName = (l) => (typeof l === 'string' ? l : `L${l}`);
const range = (a, b) => `${Number(a).toFixed(0)}–${Number(b).toFixed(0)} m`;

/* ------------------------------------------------------------------ model dropdown (shared state across instances) */

/** Model choice shared by every spend button. Default preselected, never remembered across reloads. */
export function createModels() {
  const ctl = { data: null, current: null, selects: new Set(), failed: false };
  const labelOf = (id) => ((ctl.data && ctl.data.options.find((o) => o.id === id)) || {}).label || id || '';
  ctl.value = () => ctl.current;
  /** Programmatic choice (the Max quality preset); only an enabled model can be set. Returns whether it was applied. */
  ctl.set = (id) => {
    const opt = ctl.data && ctl.data.options.find((x) => x.id === id);
    if (!opt || !opt.enabled) return false;
    ctl.current = id;
    for (const fn of ctl.selects) fn();
    if (ctl.onChange) ctl.onChange();
    return true;
  };
  ctl.label = () => labelOf(ctl.current);
  ctl.load = async () => {
    try {
      ctl.data = await aget('/api/admin/models');
      ctl.current = ctl.data.default;
      ctl.failed = false;
    } catch (err) {
      ctl.failed = true; ctl.error = err;
      if (err && err.status === 401) throw err;
    }
    for (const fn of ctl.selects) fn();
  };
  /** Render a labelled model dropdown (component: model_select.js) into `container`; all instances stay in sync. */
  ctl.mount = (container) => {
    if (!container) return;
    const label = el('label', { class: 'model-label' }, ['Model ']);
    const note = el('small', { class: 'muted model-note', 'data-testid': 'model-note' });
    let select = null;
    const draw = () => {
      if (!ctl.data) {
        label.hidden = true; note.textContent = ctl.failed ? 'Model list unavailable — the server default will be used.' : '';
        return;
      }
      if (!select) {
        select = renderModelSelect(ctl.data);
        select.addEventListener('change', () => {
          const opt = ctl.data.options.find((x) => x.id === select.value);
          if (!opt || !opt.enabled) { select.value = ctl.current; return; } // a disabled model can never be chosen
          ctl.current = select.value;
          for (const fn of ctl.selects) fn();
          if (ctl.onChange) ctl.onChange();
        });
        label.append(select);
      }
      label.hidden = false;
      select.value = ctl.current;
      const dis = ctl.data.options.filter((o) => !o.enabled);
      note.textContent = dis.length ? dis.map((o) => `${o.label}: ${o.disabled_reason || 'not available'}`).join(' ') : '';
    };
    ctl.selects.add(draw);
    container.replaceChildren(label, note);
    draw();
  };
  return ctl;
}

/* ------------------------------------------------------------------ weakest intervals + header + help */

function levelBar(level) {
  const lvl = levelName(level);
  const n = typeof level === 'number' ? level : Number(String(level).slice(1));
  return el('span', { class: 'lvl-bar', 'data-level': lvl, role: 'img', 'aria-label': `${lvl} of L${LEVEL_MAX}` },
    el('i', { style: `width:${Math.max(6, (100 * n) / LEVEL_MAX)}%` }));
}

export function renderWeakest(box, plan, model, { onFill, onUpload }) {
  const bar = el('div', { class: 'cov-strip improve-bar', 'data-testid': 'improve-bar', role: 'img', 'aria-label': 'Fidelity along the street' },
    ((model && model.fidelity && model.fidelity.bins) || []).map((b) => {
      const len = model.fidelity.length_s || 1;
      return el('span', { class: 'cov-cell', 'data-level': levelName(b.level), style: `width:${(100 * (b.s_to - b.s_from)) / len}%`, title: `${b.s_from}–${b.s_to} m · ${levelName(b.level)}` });
    }));
  const items = plan.weakest || [];
  let body;
  if (plan.head_version == null) {
    body = el('p', { class: 'muted', 'data-testid': 'improve-nomodel', text: (plan.steps && plan.steps[0]) || 'Run Check placement, then Generate once to create the first model.' });
  } else if (!items.length) {
    body = el('p', { class: 'notice ok', 'data-testid': 'improve-weakest-empty', text: 'Every stretch is at the target level.' });
  } else {
    body = el('ol', { class: 'weakest-list' }, items.map((iv, i) => el('li', { class: 'weak-item', 'data-testid': 'weakest-item', 'data-s-from': iv.s_from, 'data-s-to': iv.s_to, 'data-level': levelName(iv.level) }, [
      el('div', { class: 'weak-head' }, [
        el('strong', { text: `${range(iv.s_from, iv.s_to)} · ${levelName(iv.level)}` }), ' ', levelBar(iv.level),
        iv.weakest_reason ? el('span', { class: 'muted', text: ` · ${iv.weakest_reason}` }) : null,
      ]),
      el('p', { class: 'weak-hint', text: `→ ${iv.hint}` }),
      el('div', { class: 'weak-actions' }, [
        el('button', { type: 'button', class: 'secondary small', 'data-testid': 'fill-stretch', text: 'Fill this stretch', onclick: () => onFill(iv) }),
        el('button', { type: 'button', class: 'secondary small', 'data-testid': 'upload-here', text: 'Upload here', onclick: () => onUpload(iv) }),
      ]),
    ])));
  }
  box.replaceChildren(
    el('h3', { text: 'Where it is weakest' }),
    plan.head_version == null ? null : bar,
    body,
  );
}

export function renderHelp(box, plan) {
  box.replaceChildren(
    el('summary', { text: 'How to add to an existing model' }),
    el('ol', { class: 'help-steps', 'data-testid': 'improve-steps' }, (plan.steps || []).map((t) => el('li', { text: t.replace(/^\d+\.\s*/, '') }))),
    el('p', { class: 'muted', text: 'Cost: Street View images are $0.007 each, satellite crops $0.002; Claude adds a few cents per run. Uploads and Regenerate with what we have cost nothing by themselves.' }),
    el('p', { class: 'muted', text: 'Fidelity levels: L0 map and satellite only · L1 one viewpoint · L2 several viewpoints, both sides · L3 plus a close-up photo.' }),
  );
}

/* ------------------------------------------------------------------ the panel */

/**
 * @param {object} o
 *  root: #sec-improve, slug, models (createModels), getModel(): model detail, activeReason(): string, getActiveRunId(): string|null,
 *  onAuth(err): bool, onFillRange(iv) / onUploadHere(iv): callbacks into the other cards, onRegenerated(): Promise
 */
export function createImprove(o) {
  const { root, slug, models } = o;
  const q = (t) => root.querySelector(`[data-testid=${t}]`);
  const fill = q('improve-action-fill_gaps');
  const regen = q('improve-action-regenerate');
  const previewBtn = fill.querySelector('[data-testid=fill-preview]');
  const fgGen = fill.querySelector('[data-testid=fill-generate]');
  const fgFetch = fill.querySelector('[data-testid=fill-fetch]');
  const out = fill.querySelector('[data-testid=fill-result]');
  const fmsg = fill.querySelector('.form-msg');
  const rangeNote = fill.querySelector('.fill-range');
  const regBtn = regen.querySelector('[data-testid=regenerate]');
  const regOut = regen.querySelector('[data-testid=regenerate-result]');
  const regHint = regen.querySelector('.regen-hint');
  let plan = null;
  let preview = null; // {body, data}
  let fillRange = null;

  models.mount(fill.querySelector('.model-slot'));
  // an edit of the quality fields invalidates the shown estimate: re-plan when a preview is on screen
  const quality = createQuality({ container: fill.querySelector('.quality-slot'), models, testid: 'fill',
    onChange: () => { if (preview) doPreview(); } });

  function body() {
    const base = (plan && plan.actions.fill_gaps && plan.actions.fill_gaps.default_body) || {};
    const b = { ...base };
    if (fillRange) b.s_range = { s_from: fillRange.s_from, s_to: fillRange.s_to };
    if (models.value()) b.model = models.value(); // the estimate is priced with the chosen model
    const q = quality.value();
    if (q) b.quality = q;
    return b;
  }

  function busyUpdate() {
    const reason = o.activeReason();
    const hasModel = !!(plan && plan.head_version != null);
    const avail = !plan || (plan.actions.fill_gaps || {}).available !== false;
    previewBtn.disabled = !hasModel || !avail;
    previewBtn.title = !hasModel ? 'Generate once to create the first model' : '';
    const why = reason || (!preview ? 'Press Preview plan first.' : '');
    for (const b of [fgGen, fgFetch]) { b.disabled = !!why; b.title = why; }
    const rc = plan && plan.actions.regenerate_cached;
    regBtn.disabled = !!reason || !hasModel || (rc && rc.available === false);
    regBtn.title = reason || (!hasModel ? 'Nothing to regenerate: generate once first.' : 'Rebuild the scene from stored data: $0');
    const note = root.querySelector('.busy-note');
    const id = o.getActiveRunId && o.getActiveRunId();
    note.hidden = !reason;
    note.replaceChildren(reason);
    if (id) note.append(' ', el('a', { href: `/admin/run.html?id=${encodeURIComponent(id)}`, text: 'View the running run' }));
  }

  function drawRange() {
    rangeNote.hidden = !fillRange;
    rangeNote.replaceChildren();
    if (fillRange) {
      rangeNote.append(`Only ${range(fillRange.s_from, fillRange.s_to)} `, el('button', { type: 'button', class: 'secondary small', text: 'Whole street', onclick: () => { fillRange = null; preview = null; out.replaceChildren(); drawRange(); busyUpdate(); } }));
    }
  }

  function drawPreview(data) {
    const rows = data.images || [];
    const c = data.counts || {};
    const est = data.est || {};
    const totals = el('p', { class: 'fill-totals', 'data-testid': 'fill-totals',
      text: `${c.new} new · ${c.cached} cached · ≈ ${fmtUsd(est.google_usd)} Google · ≈ ${fmtUsd(est.claude_usd)} Claude (estimate${data.quality && data.quality.preset ? `, up to ${data.quality.extraction_passes} passes + ${data.quality.review_rounds} review rounds` : ''}) · ≈ ${fmtUsd(est.total_usd)} total · Model: ${models.label() || 'server default'}` });
    const chips = [];
    if (data.trimmed) chips.push(el('span', { class: 'chip warn', 'data-testid': 'fill-trimmed', text: 'trimmed to the image cap' }));
    if (data.will_exceed_budget) chips.push(el('span', { class: 'chip warn', 'data-testid': 'fill-exceeds', text: 'estimate exceeds the remaining global budget' }));
    if (!rows.length) {
      out.replaceChildren(el('p', { class: 'notice ok', 'data-testid': 'fill-empty', text: data.note || 'Nothing to fill: every stretch is at the target level.' }));
      return;
    }
    const targeted = (data.intervals_targeted || []).map((t) => el('li', { text: `${range(t.s_from, t.s_to)}: L${t.from_level} → L${t.to_level}` }));
    out.replaceChildren(
      el('p', { class: 'muted', text: 'Stretches this plan improves:' }),
      el('ul', { class: 'fill-stretches', 'data-testid': 'fill-stretches' }, targeted),
      el('div', { class: 'table-wrap' }, el('table', { class: 'fill-table', 'data-testid': 'fill-table' }, [
        el('thead', {}, el('tr', {}, ['Kind', 'View', 'Station', 'New / cached', 'Reason'].map((t) => el('th', { text: t })))),
        el('tbody', {}, rows.map((r) => el('tr', { 'data-cached': String(!!r.cached) }, [
          el('td', { text: r.kind.replace('satellite_', 'sat ') }), el('td', { text: r.view || (r.zoom ? `zoom ${r.zoom}` : '—') }),
          el('td', { text: `${Number(r.station_s).toFixed(0)} m` }),
          el('td', {}, el('span', { class: `chip ${r.cached ? 'ready' : 'queued'}`, text: r.cached ? 'cached' : 'new' })),
          el('td', { text: r.reason || '' }),
        ]))),
      ])),
      totals, ...chips, data.note ? el('p', { class: 'muted small-print', text: data.note }) : null,
    );
  }

  async function doPreview() {
    fmsg.hidden = true; previewBtn.disabled = true; previewBtn.textContent = 'Planning…';
    out.replaceChildren(el('p', { class: 'muted', text: 'Planning…' }));
    try {
      const b = body();
      const data = await apost(segPath(slug, '/fill-gaps/preview'), b);
      preview = { body: b, data };
      drawPreview(data);
      if (!(data.images || []).length) preview = null;
    } catch (err) {
      preview = null; out.replaceChildren();
      if (o.onAuth(err)) return;
      fmsg.className = 'form-msg notice error'; fmsg.textContent = `Couldn't plan — ${err.message}`; fmsg.hidden = false;
    } finally {
      previewBtn.textContent = 'Preview plan'; busyUpdate();
    }
  }

  async function doRun(fuse) {
    if (!preview) return;
    fmsg.hidden = true; fgGen.disabled = true; fgFetch.disabled = true;
    try {
      const b = { ...preview.body, fuse };
      if (fuse && models.value()) b.model = models.value(); else delete b.model;
      if (!fuse) delete b.quality;
      const data = await apost(segPath(slug, '/fill-gaps'), b);
      location.href = `/admin/run.html?id=${encodeURIComponent(data.run_id)}`;
    } catch (err) {
      if (o.onAuth(err)) return;
      fmsg.className = 'form-msg notice error';
      fmsg.textContent = err.code === 'run_in_progress' ? 'Another run is in progress — wait for it to finish.'
        : err.code === 'budget_exhausted' ? err.message : `Couldn't start the run — ${err.message}`;
      fmsg.hidden = false; busyUpdate();
    }
  }

  async function doRegenerate() {
    const mode = root.querySelector('[name=regen-mode]:checked').value;
    regBtn.disabled = true; regBtn.textContent = 'Regenerating…';
    regOut.className = 'regen-result muted'; regOut.textContent = 'Rebuilding the scene from stored data…';
    try {
      const d = await apost(segPath(slug, '/regenerate'), { mode });
      let text = `${d.unchanged && !d.version_created ? 'Nothing changed' : 'Done'} — scene v${d.scene_schema}, 0 Google requests, 0 Claude calls, ${fmtUsd(d.cost_usd)}`;
      if (d.interpolated_m) text += `; ${Number(d.interpolated_m).toFixed(0)} m interpolated`;
      if (d.dynamic_excluded) text += `; ${d.dynamic_excluded} objects now spawn dynamically`;
      if (d.version_created) text += `; new model version v${d.version}`;
      regOut.className = 'regen-result notice ok';
      regOut.textContent = text;
      if (d.warnings && d.warnings.length) regOut.append(el('br'), d.warnings.join(' '));
      if (d.missing_extractions && d.missing_extractions.length) regOut.append(el('br'), `${d.missing_extractions.length} stored photos have no saved detections and stay unfused (Generate fuses them).`);
      await o.onRegenerated();
    } catch (err) {
      if (o.onAuth(err)) return;
      regOut.className = 'regen-result notice error'; regOut.setAttribute('role', 'alert');
      regOut.textContent = `Couldn't regenerate — ${err.message}`;
    } finally {
      regBtn.textContent = 'Regenerate'; busyUpdate();
    }
  }

  previewBtn.addEventListener('click', doPreview);
  fgGen.addEventListener('click', () => doRun(true));
  fgFetch.addEventListener('click', () => doRun(false));
  regBtn.addEventListener('click', doRegenerate);
  models.onChange = () => { if (preview) doPreview(); }; // the price depends on the model: ask the server again

  return {
    /** Re-render the data-driven bits after the plan / model changed (the cards themselves persist). */
    update(newPlan, model) {
      plan = newPlan;
      renderWeakest(q('improve-weakest'), plan, model, {
        onFill: (iv) => { fillRange = { s_from: iv.s_from, s_to: iv.s_to }; preview = null; drawRange(); fill.scrollIntoView({ block: 'center' }); doPreview(); },
        onUpload: (iv) => o.onUploadHere(iv),
      });
      renderHelp(q('improve-help'), plan);
      const schema = plan.scene_schema;
      const sc = q('improve-scene');
      if (schema == null) sc.textContent = '';
      else if (schema < 3) { sc.textContent = `scene v${schema} (stale — Regenerate to upgrade)`; sc.className = 'chip warn'; }
      else { sc.textContent = `scene v${schema}`; sc.className = 'chip ready'; }
      const rc = plan.actions.regenerate_cached || {};
      regHint.hidden = !(rc.missing_extractions > 0);
      regHint.textContent = rc.missing_extractions > 0
        ? `${rc.missing_extractions} stored photo${rc.missing_extractions === 1 ? ' has' : 's have'} no saved detections: “Also re-merge” leaves them unfused (they need Generate, which costs Claude).` : '';
      drawRange(); busyUpdate();
    },
    busyUpdate,
    setPlanFailed() { plan = null; busyUpdate(); },
    setBadge(text) { q('improve-badge').textContent = text; },
    /** Pre-set the fill-gaps range (also used by the coverage popover). */
    setRange(a, b) { fillRange = { s_from: a, s_to: b }; preview = null; drawRange(); busyUpdate(); },
    get preview() { return preview; },
  };
}
