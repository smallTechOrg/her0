// Reusable per-run model dropdown (spec/ui.md "Model dropdown"). Imported by the admin pages that start an LLM run
// (Generate, Fetch + Generate, Fill gaps). Data: GET /api/admin/models -> {default, options[]}.
//
//   const ms = await mountModelSelect(containerEl, { onChange: (id) => ... });
//   body.model = ms.value();   // undefined when the default is selected -> the server uses HERO_VISION_MODEL
//
// The default is preselected on every mount; a non-default choice is never remembered across reloads.

import { aget, el } from '/js/admin/auth.js?v=5';

const priceText = (o) => `$${o.input_usd_per_mtok}/Mtok in · $${o.output_usd_per_mtok}/Mtok out`;

export function optionTitle(o) {
  if (!o.enabled) return o.disabled_reason || 'Not available';
  const src = o.price_verified_on ? ` (price verified ${o.price_verified_on})` : '';
  return `${priceText(o)}${src}`;
}

/** Build the <select data-testid="model-select"> for an already-loaded {default, options} payload. */
export function renderModelSelect(models, { onChange, id } = {}) {
  const select = el('select', { 'data-testid': 'model-select', 'aria-label': 'Model', class: 'model-select', id });
  for (const o of models.options) {
    const opt = el('option', {
      value: o.id,
      text: o.enabled ? o.label : `${o.label} — unavailable`,
      title: optionTitle(o),
      disabled: !o.enabled,
      'data-enabled': o.enabled ? 'true' : 'false',
    });
    select.append(opt);
  }
  const fallback = models.options.find((o) => o.id === models.default && o.enabled) || models.options.find((o) => o.enabled);
  if (fallback) select.value = fallback.id;
  const sync = () => {
    const o = models.options.find((x) => x.id === select.value);
    select.title = o ? optionTitle(o) : '';
    if (onChange) onChange(select.value, o);
  };
  select.addEventListener('change', sync);
  select.title = fallback ? optionTitle(fallback) : '';
  return select;
}

/**
 * Fetch the options and render the dropdown into `container`. Returns {select, models, value()}; `value()` is the
 * chosen id, or undefined when it is the default (so callers simply omit `model`). On a load error a small inline
 * message replaces the dropdown and value() stays undefined (= the default).
 */
export async function mountModelSelect(container, { onChange, fetchModels = () => aget('/api/admin/models') } = {}) {
  let models;
  try {
    models = await fetchModels();
  } catch (err) {
    container.replaceChildren(el('span', { class: 'muted', 'data-testid': 'model-select-error',
      text: `Model list unavailable (${err.message || err}); the default model will be used.` }));
    return { select: null, models: null, value: () => undefined };
  }
  const select = renderModelSelect(models, { onChange });
  container.replaceChildren(select);
  return { select, models, value: () => (select.value && select.value !== models.default ? select.value : undefined) };
}
