// Placement preview rendering shared by /admin/ (after Create segment) and the segment page.
// API: POST /api/admin/segments/{slug}/placement-preview -> {url, reference_line, placement_verified, warnings[]}.

import { el, apost, segPath } from '/js/admin/auth.js?v=5';

const OSM_SOURCES = new Set(['osm_nominatim', 'osm_overpass']);

export function referenceLineText(ref, verified) {
  if (ref && OSM_SOURCES.has(ref.source)) {
    const way = ref.osm_way_id ? `OpenStreetMap way ${ref.osm_way_id}` : 'OpenStreetMap road';
    const name = ref.name ? ` (${ref.name})` : '';
    const snap = ref.snap_m != null ? ` · line snapped ${Number(ref.snap_m).toFixed(0)} m from the address` : '';
    return { ok: verified !== false, text: `Road geometry: ${way}${name}${snap}` };
  }
  if (ref && ref.source === 'legacy_straight') {
    return { ok: false, text: 'Legacy straight line — placement not verified' };
  }
  return { ok: false, text: 'Road geometry unavailable — using a straight line; placement not verified' };
}

/** Fill `box` with the overview tile + the reference-line verdict. */
export function renderPlacement(box, data) {
  const { ok, text } = referenceLineText(data.reference_line, data.placement_verified);
  const ref = data.reference_line || {};
  box.replaceChildren(
    el('figure', { class: 'placement-fig' }, [
      data.url ? el('img', { src: data.url, alt: 'Overview tile with the traced reference line, 10 m ticks and footpath side' }) : null,
      el('figcaption', { text: 'Traced reference line, 10 m ticks and the footpath side on the overview tile.' }),
    ]),
    el('p', { class: ok ? 'placement-verdict ok' : 'placement-verdict error', 'data-testid': 'placement-verdict', text }),
    ref.length_s != null ? el('p', { class: 'muted', text: `Line length ${Number(ref.length_s).toFixed(1)} m${ref.extrapolated_m && (ref.extrapolated_m.start || ref.extrapolated_m.end) ? ` · extended ${ref.extrapolated_m.start + ref.extrapolated_m.end} m straight` : ''}` }) : null,
    ...((data.warnings || []).map((w) => el('p', { class: 'notice error', text: w }))),
  );
  box.hidden = false;
}

/** Wire a button to run the preview into `box`. */
export function placementButton(slug, { box, onError, label = 'Check placement' }) {
  const btn = el('button', { type: 'button', 'data-testid': 'check-placement', text: label });
  btn.addEventListener('click', async () => {
    btn.disabled = true; btn.textContent = 'Checking…';
    try {
      renderPlacement(box, await apost(segPath(slug, '/placement-preview')));
    } catch (err) {
      if (onError && onError(err)) return;
      box.replaceChildren(el('p', { class: 'error', role: 'alert', text: `Couldn't check placement — ${err.message}` }));
      box.hidden = false;
    } finally {
      btn.disabled = false; btn.textContent = label;
    }
  });
  return btn;
}
