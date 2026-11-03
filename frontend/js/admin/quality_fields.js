// "Max quality" run preset (spec/ui.md): a checkbox (default OFF) plus the editable per-run quality fields.
// API: GET /api/admin/quality-preset -> {preset: {model, sv_spacing_m, headings, sat_crop_zoom, sat_extra_zoom, max_images,
// extraction_passes, review_rounds, review_stop_changes, frames_per_station}, limits}. The server is the one source of the values.
//
// Selecting the preset fills the fields below (and, for the Add imagery form, its spacing / headings / zoom / second tile set
// through `onPreset`), and switches the shared model dropdown to the preset model when that model is enabled. Leaving the
// checkbox unselected never touches the model. There is no dollar cap per run: only the global budget hard stop applies.

import { el, aget } from '/js/admin/auth.js?v=5';

let presetPromise = null;
export const loadPreset = () => { if (!presetPromise) presetPromise = aget('/api/admin/quality-preset').catch((e) => { presetPromise = null; throw e; }); return presetPromise; };

/**
 * @param {object} o  container: element to fill; models: createModels() ctl; testid: suffix for data-testids;
 *   onPreset(preset, on): fill extra controls of the host form; onChange(): the host re-estimates / re-previews.
 * @returns {{value(): object|undefined, on(): boolean, fields: object, ready: Promise}}
 */
export function createQuality({ container, models, testid = 'q', onPreset, onChange, defaultOn = true }) {
  const cb = el('input', { type: 'checkbox', 'data-testid': `max-quality-${testid}`, name: 'max_quality', disabled: true });
  const passes = el('input', { type: 'number', min: '1', max: '5', step: '1', value: '1', name: 'extraction_passes', 'data-testid': `q-passes-${testid}` });
  const rounds = el('input', { type: 'number', min: '0', max: '8', step: '1', value: '', placeholder: 'default', name: 'review_rounds', 'data-testid': `q-rounds-${testid}` });
  const note = el('p', { class: 'muted q-note', 'data-testid': `q-note-${testid}` });
  const fields = el('div', { class: 'q-fields', 'data-testid': `q-fields-${testid}`, hidden: true }, [
    el('label', {}, ['Extraction passes per frame', passes]),
    el('label', {}, ['Review rounds (up to)', rounds]),
  ]);
  const box = el('div', { class: 'quality-box', 'data-testid': `quality-${testid}` }, [
    el('label', { class: 'inline' }, [cb, 'Full detail (default)']),
    el('small', { class: 'muted' }, 'Every panorama at full detail, more extraction passes and review rounds. Untick for a cheap, quick pass.'),
    fields, note,
  ]);
  container.replaceChildren(box);
  let preset = null;
  let before = null; // model that was selected before the preset switched it
  let pendingApply = false; // the preset arrived before the model list: switch the model once it is there

  const clamp = (inp, lo, hi) => {
    const n = Number.parseInt(inp.value, 10);
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : null;
  };

  function apply() {
    const on = cb.checked;
    box.classList.toggle('on', on);
    fields.hidden = !on;
    if (!preset) return;
    if (on) {
      passes.value = String(preset.extraction_passes);
      rounds.value = String(preset.review_rounds);
      const want = preset.model;
      if (!models.data) { pendingApply = true; note.textContent = ''; if (onPreset) onPreset(preset, on); if (onChange) onChange(); return; }
      const opt = models.data.options.find((x) => x.id === want);
      if (opt && opt.enabled) {
        if (models.value() !== want) { before = models.value() || (models.data && models.data.default) || null; models.set(want); }
        note.textContent = `Model set to ${opt.label}; you can switch it back in the dropdown.`;
      } else {
        note.textContent = `${want} is not enabled${opt && opt.disabled_reason ? ` (${opt.disabled_reason})` : ''}; the chosen model is kept.`;
      }
    } else {
      if (before && models.value() === preset.model) models.set(before);
      before = null;
      note.textContent = '';
    }
    if (onPreset) onPreset(preset, on);
    if (onChange) onChange();
  }

  cb.addEventListener('change', apply);
  // One "Full detail" switch per page: the Fill-gaps and Add-imagery boxes follow each other (they share the model).
  if (models) {
    models.qualityBoxes = models.qualityBoxes || new Set();
    models.qualityBoxes.add(cb);
    cb.addEventListener('change', () => {
      for (const other of models.qualityBoxes) if (other !== cb && other.checked !== cb.checked) { other.checked = cb.checked; other.dispatchEvent(new Event('change')); }
    });
  }
  if (models && models.selects) models.selects.add(() => { if (pendingApply && models.data) { pendingApply = false; if (cb.checked) apply(); } });
  for (const inp of [passes, rounds]) inp.addEventListener('input', () => { if (onChange) onChange(); });

  const ready = loadPreset().then((d) => {
    preset = d.preset;
    const lim = d.limits || {};
    if (lim.extraction_passes) { passes.min = String(lim.extraction_passes[0]); passes.max = String(lim.extraction_passes[1]); }
    if (lim.review_rounds) { rounds.min = String(lim.review_rounds[0]); rounds.max = String(lim.review_rounds[1]); }
    cb.disabled = false;
    if (defaultOn && !cb.checked) cb.checked = true;   // the default is the highly detailed rendition
    if (cb.checked) apply();                           // (also when it was ticked before the preset arrived)
  }).catch((err) => { note.textContent = `Max quality is unavailable (${err.message || err}).`; });

  return {
    ready, fields: { passes, rounds, checkbox: cb },
    on: () => cb.checked,
    /** The `quality` request object, or undefined when the preset is off (the request is then exactly as before). */
    value() {
      if (!cb.checked || !preset) return undefined;
      const q = { preset: true };
      const p = clamp(passes, 1, 5);
      const r = clamp(rounds, 0, 8);
      if (p != null) q.extraction_passes = p;
      if (r != null) q.review_rounds = r;
      return q;
    },
    get preset() { return preset; },
  };
}
