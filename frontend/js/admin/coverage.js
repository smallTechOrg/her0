// Coverage / fidelity map: one cell per 5 m from FidelityMap.bins (never recomputed here), s-ticks every 10 m,
// footpath-state row from the head profile, entity markers, legend + histogram, click -> popover.
// API: GET .../model/coverage?version= (FidelityMap + observations_by_bin).

import { el } from '/js/admin/auth.js?v=5';

export const LEVEL_NAMES = ['L0', 'L1', 'L2', 'L3'];
const LEVEL_TEXT = {
  L0: 'satellite + map only', L1: 'one viewpoint', L2: 'multi-view', L3: 'multi-view + close-up photo',
};
const CLASS_BY_TYPE = {
  tree: 'obstacle', pole: 'obstacle', parked_vehicle: 'obstacle', signboard: 'obstacle', bollard: 'obstacle', bin: 'obstacle',
  vendor_stall: 'obstacle', railing: 'obstacle', bus_stop: 'obstacle', gate: 'obstacle', other: 'obstacle',
  drain: 'hazard', pothole: 'hazard', debris: 'hazard', speed_breaker: 'hazard', low_branch: 'hazard',
  wall: 'scenery', shopfront: 'scenery', manhole: 'marking', crossing: 'marking', ramp: 'marking', kerb_cut: 'marking',
};
export const entityClass = (type) => CLASS_BY_TYPE[type] || 'obstacle';

const levelName = (l) => (typeof l === 'string' ? l : LEVEL_NAMES[l] || 'L0');

function stateAt(profile, s) {
  const iv = (profile || []).find((p) => s >= p.s_from && s < p.s_to) || (profile || [])[profile.length - 1];
  return iv ? iv.footpath_state : 'unknown';
}

export function histogramText(hist) {
  const parts = Object.entries(hist || {}).filter(([, v]) => v > 0).map(([k, v]) => `${Number(v).toFixed(0)} m ${k}`);
  return parts.length ? parts.join(' · ') : 'no coverage data';
}

/**
 * @param {HTMLElement} root  the [data-testid=coverage] section body
 * @param {object} o {coverage: FidelityMap+observations_by_bin, obsById: Map, profile, entities, onPickRange(s_from,s_to)}
 */
export function renderCoverage(root, o) {
  const f = o.coverage;
  const bins = f.bins || [];
  const length = f.length_s || (bins.length ? bins[bins.length - 1].s_to : 0);
  const byBin = new Map((f.observations_by_bin || []).map((b) => [b.index, b.observation_ids || []]));
  const pctOf = (s) => `${(100 * s) / length}%`;

  const inner = el('div', { class: 'cov-inner', style: `min-width:${Math.max(bins.length * 14, 280)}px` });
  const ticks = el('div', { class: 'cov-ticks', 'aria-hidden': 'true' });
  for (let s = 0; s <= length + 1e-6; s += 10) {
    ticks.append(el('span', { class: 'cov-tick', style: `left:${pctOf(s)}`, text: `${s}` }));
  }
  const strip = el('div', { class: 'cov-strip', role: 'group', 'aria-label': 'Fidelity along the street, one cell per 5 m' });
  const fp = el('div', { class: 'cov-strip fp-strip', 'aria-label': 'Footpath state along the street' });
  const markers = el('div', { class: 'cov-markers' });
  const popover = el('div', { class: 'cov-popover', 'data-testid': 'coverage-popover', hidden: true, role: 'region', 'aria-live': 'polite' });
  let selected = null;

  for (const b of bins) {
    const lvl = levelName(b.level);
    const w = `${(100 * (b.s_to - b.s_from)) / length}%`;
    const cell = el('button', {
      type: 'button', class: 'cov-cell', 'data-level': lvl, 'data-index': b.index, style: `width:${w}`,
      title: `${b.s_from}–${b.s_to} m · ${lvl}`, 'aria-label': `${b.s_from} to ${b.s_to} metres, ${lvl}, ${LEVEL_TEXT[lvl]}`,
    });
    cell.addEventListener('click', () => {
      if (selected) selected.removeAttribute('aria-pressed');
      selected = cell; cell.setAttribute('aria-pressed', 'true');
      showPopover(b, lvl);
    });
    strip.append(cell);
    const st = stateAt(o.profile, (b.s_from + b.s_to) / 2);
    fp.append(el('span', { class: 'fp-cell', 'data-fp': st, style: `width:${w}`, title: `${b.s_from}–${b.s_to} m · footpath ${st}` }));
  }

  for (const e of o.entities || []) {
    markers.append(el('span', {
      class: `ent-marker cls-${entityClass(e.type)}`, 'data-entity-id': e.id, style: `left:${pctOf(e.s)};opacity:${Math.max(0.25, e.confidence)}`,
      title: `${e.type} at ${Number(e.s).toFixed(1)} m · ${Math.round(100 * e.confidence)}%`,
    }));
  }

  function showPopover(b, lvl) {
    const ids = byBin.get(b.index) || [];
    const thumbs = ids.map((id) => o.obsById && o.obsById.get(id)).filter(Boolean);
    popover.replaceChildren(
      el('h3', { text: `${b.s_from}–${b.s_to} m · ${lvl} (${LEVEL_TEXT[lvl]})` }),
      el('p', { class: 'muted', text: `${b.views} view${b.views === 1 ? '' : 's'} · ${b.stations} station${b.stations === 1 ? '' : 's'} · left ${b.sides ? b.sides.left : 0} / right ${b.sides ? b.sides.right : 0}${b.overhead_hires ? ' · hi-res overhead crop' : ''}` }),
      el('p', { class: 'hint', 'data-testid': 'coverage-hint', text: b.hint ? `To improve: ${b.hint}` : 'Nothing further needed for the next level here.' }),
      el('div', { class: 'thumbs' }, thumbs.length
        ? thumbs.map((ob) => el('a', { href: ob.url, target: '_blank', rel: 'noopener', title: `${ob.kind} · ${ob.view || ''}` },
            el('img', { src: ob.url, alt: `${ob.kind} ${ob.view || ''} photo covering ${b.s_from}–${b.s_to} m`, loading: 'lazy' })))
        : [el('p', { class: 'muted', text: 'No photos cover this stretch yet.' })]),
      o.onPickRange ? el('button', {
        type: 'button', class: 'secondary small', 'data-testid': 'use-range', text: 'Add imagery for this stretch',
        onclick: () => o.onPickRange(b.s_from, b.s_to),
      }) : null,
    );
    popover.hidden = false;
  }

  inner.append(ticks, strip, fp, markers);
  const legend = el('ul', { class: 'cov-legend' }, [
    ...LEVEL_NAMES.map((n) => el('li', {}, [el('span', { class: 'sw', 'data-level': n }), `${n} ${LEVEL_TEXT[n]}`])),
  ]);
  const fpLegend = el('ul', { class: 'cov-legend' }, ['intact', 'broken', 'missing', 'blocked', 'encroached', 'unknown']
    .map((n) => el('li', {}, [el('span', { class: 'sw fp-cell', 'data-fp': n }), `footpath ${n}`])));
  const hist = el('p', { class: 'cov-hist', 'data-testid': 'coverage-histogram', text: histogramText(f.overall && f.overall.histogram_m) });
  root.replaceChildren(
    el('div', { class: 'cov-scroll' }, inner),
    hist,
    popover,
    legend,
    fpLegend,
    el('p', { class: 'muted small-print', text: 'Top row: fidelity per 5 m. Second row: footpath state. Markers: shape by class (square obstacle, triangle hazard, circle marking, diamond scenery), fainter = less confident. Click a cell for photos and what would improve it.' }),
  );
}
