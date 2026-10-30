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
export function createQuality({ container, models, testid = 'q', onPreset, onChange }) {
  const cb = el('input', { type: 'checkbox', 'data-testid': `max-quality-${testid}`, name: 'max_quality', disabled: true });
  const passes = el('input', { type: 'number', min: '1', max: '5', step: '1', value: '1', name: 'extraction_passes', 'data-testid': `q-passes-${testid}` });
  const rounds = el('input', { type: 'number', min: '0', max: '8', step: '1', value: '', placeholder: 'default', name: 'review_rounds', 'data-testid': `q-rounds-${testid}` });
  const note = el('p', { class: 'muted q-note', 'data-testid': `q-note-${testid}` });
  const fields = el('div', { class: 'q-fields', 'data-testid': `q-fields-${testid}`, hidden: true }, [
    el('label', {}, ['Extraction passes per frame', passes]),
    el('label', {}, ['Review rounds (up to)', rounds]),
  ]);
  const box = el('div', { class: 'quality-box', 'data-testid': `quality-${testid}` }, [
    el('label', { class: 'inline' }, [cb, 'Max quality']),
    el('small', { class: 'muted' }, 'More frames, more extraction passes and review rounds: costs more tokens, every result is merged into the model.'),
    fields, note,
  ]);
  container.replaceChildren(box);
  let preset = null;
  let before = null; // model that was selected before the preset switched it

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
      const opt = models.data && models.data.options.find((x) => x.id === want);
      if (opt && opt.enabled) {
        if (models.value() !== want) { before = models.value(); models.set(want); }
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
  for (const inp of [passes, rounds]) inp.addEventListener('input', () => { if (onChange) onChange(); });

  const ready = loadPreset().then((d) => {
    preset = d.preset;
    const lim = d.limits || {};
    if (lim.extraction_passes) { passes.min = String(lim.extraction_passes[0]); passes.max = String(lim.extraction_passes[1]); }
    if (lim.review_rounds) { rounds.min = String(lim.review_rounds[0]); rounds.max = String(lim.review_rounds[1]); }
    cb.disabled = false;
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
