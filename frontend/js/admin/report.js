// Run report (GET /api/admin/runs/{id}/report -> RunReport, spec/data.md) + "Images used" grid.

import { el, fmtUsd, fmtUsdPrecise, pct } from '/js/admin/auth.js?v=4';

const LEVELS = ['L0', 'L1', 'L2', 'L3'];

function fidBar(hist, label) {
  const total = LEVELS.reduce((a, k) => a + (hist[k] || 0), 0) || 1;
  return el('div', { class: 'fid-row' }, [
    el('span', { class: 'fid-label', text: label }),
    el('div', { class: 'cov-strip fid-bar', role: 'img', 'aria-label': `${label}: ${LEVELS.filter((k) => hist[k]).map((k) => `${hist[k]} m ${k}`).join(', ') || 'no data'}` },
      LEVELS.filter((k) => hist[k] > 0).map((k) => el('span', { class: 'fid-seg', 'data-level': k, style: `width:${(100 * hist[k]) / total}%`, text: `${Math.round(hist[k])} m ${k}` }))),
  ]);
}

const num = (x) => (x == null ? '—' : String(x));

export function renderReport(root, r, { slug } = {}) {
  const im = r.images || {};
  const det = r.detections || {};
  const link = (id, text) => (slug ? el('a', { href: `/admin/segment.html?slug=${encodeURIComponent(slug)}&entity=${encodeURIComponent(id)}`, text }) : text);
  const views = ['forward', 'back', 'left', 'right'];
  const fid = r.fidelity || {};
  const cost = r.cost || {};
  const list = (title, items, testid) => el('div', { 'data-testid': testid }, [
    el('h4', { text: `${title} (${items.length})` }),
    items.length ? el('ul', {}, items) : el('p', { class: 'muted', text: 'None.' }),
  ]);
  root.replaceChildren(
    r.degraded ? el('p', { class: 'notice error', text: 'Partial report: the run ended before the report step.' }) : null,
    el('p', { class: 'report-summary', 'data-testid': 'report-summary', text: r.summary || '' }),
    el('h3', { text: 'Images' }),
    el('div', { class: 'table-wrap' }, el('table', { 'data-testid': 'report-images' }, [
      el('thead', {}, el('tr', {}, ['Total used', 'New', 'Cached', 'Fused this run', 'Planned'].map((h) => el('th', { text: h })))),
      el('tbody', {}, el('tr', {}, [
        el('td', { 'data-col': 'total', text: num(im.used_total) }), el('td', { 'data-col': 'new', text: num(im.new) }),
        el('td', { 'data-col': 'cached', text: num(im.cached) }), el('td', { 'data-col': 'fused', text: num(im.fused_this_run) }),
        el('td', { 'data-col': 'planned', text: num(im.planned) }),
      ])),
    ])),
    el('p', { class: 'muted', 'data-testid': 'report-by-source', text: `By source: ${Object.entries(im.by_source || {}).map(([k, v]) => `${k.replace('google_', '')} ${v}`).join(' · ') || '—'}` }),
    el('p', { class: 'muted', 'data-testid': 'report-by-view', text: `By heading: ${views.map((v) => `${v} ${(im.by_view || {})[v] ?? 0}`).join(' · ')}` }),
    (im.skipped || []).length ? el('p', { class: 'muted', text: `Skipped: ${im.skipped.map((s) => `${s.view || ''} @ ${s.station_s} m (${s.reason})`).join('; ')}` }) : null,
    el('h3', { text: 'What changed' }),
    list('New entities', (det.new || []).map((d) => el('li', {}, [link(d.id, d.type), ` · ${pct(d.confidence)} · ${d.description || ''}`])), 'report-new'),
    list('Changed', (det.changed || []).map((d) => el('li', {}, [link(d.id, d.id), ` — ${d.what}`])), 'report-changed'),
    list('Removed', (det.removed || []).map((d) => el('li', {}, [link(d.id, d.type || d.id), ` — ${d.description || d.what || d.reason || ''}`])), 'report-removed'),
    list('Contradicted', (det.contradicted || []).map((d) => el('li', { text: d.description || d.id })), 'report-contradicted'),
    el('p', { class: 'muted', text: `${det.unchanged_count ?? 0} unchanged.` }),
    list('Profile changes', (r.profile_changes || []).map((t) => el('li', { text: t })), 'report-profile'),
    el('h3', { text: 'Fidelity before → after' }),
    el('div', { 'data-testid': 'report-fidelity' }, [
      fidBar((fid.before || {}).histogram_m || {}, `Before (${fid.before && fid.before.overall != null ? Number(fid.before.overall).toFixed(1) : '?'})`),
      fidBar((fid.after || {}).histogram_m || {}, `After (${fid.after && fid.after.overall != null ? Number(fid.after.overall).toFixed(1) : '?'})`),
      (fid.improved_intervals || []).length
        ? el('ul', {}, fid.improved_intervals.map((i) => el('li', { text: `${i.from} → ${i.to} over ${Number(i.s_from).toFixed(0)}–${Number(i.s_to).toFixed(0)} m` })))
        : el('p', { class: 'muted', text: 'No stretch improved a level.' }),
    ]),
    el('h3', { text: 'Confidence changes' }),
    (r.confidence_changes || []).length
      ? el('div', { class: 'table-wrap' }, el('table', {}, [
          el('thead', {}, el('tr', {}, ['Entity', 'Type', 'Before', 'After', 'Why'].map((h) => el('th', { text: h })))),
          el('tbody', {}, r.confidence_changes.map((c) => el('tr', {}, [el('td', {}, link(c.entity_id, c.entity_id)), el('td', { text: c.type || '' }),
            el('td', { text: pct(c.before) }), el('td', { text: pct(c.after) }), el('td', { text: c.reason || '' })]))),
        ]))
      : el('p', { class: 'muted', text: 'No confidence changes.' }),
    el('h3', { text: 'Cost' }),
    el('dl', { class: 'kv', 'data-testid': 'report-cost' }, [
      el('dt', { text: 'Anthropic' }), el('dd', { text: `${fmtUsdPrecise(cost.anthropic_usd)} of ${fmtUsd(cost.run_cap_usd)} run cap · ${cost.anthropic_calls ?? 0} calls` }),
      el('dt', { text: 'Google' }), el('dd', { text: `${cost.google_new_requests ?? 0} new / ${cost.google_cached ?? 0} cached · est. ${fmtUsdPrecise(cost.google_est_usd)}` }),
      el('dt', { text: 'Total' }), el('dd', { text: `${fmtUsdPrecise(cost.total_usd)} · budget remaining ${fmtUsd(cost.budget_remaining_usd)}` }),
    ]),
    list('Warnings', (r.warnings || []).map((w) => el('li', { text: w })), 'report-warnings'),
    list('Suggestions', (r.suggestions || []).map((w) => el('li', { text: w })), 'report-suggestions'),
  );
}

export function renderImagesUsed(root, observations) {
  const grid = root.querySelector('.obs-grid');
  grid.replaceChildren(...observations.map((o) => el('figure', { class: 'obs', 'data-obs-id': o.id }, [
    el('a', { href: o.url, target: '_blank', rel: 'noopener' }, el('img', { src: o.url, alt: `${o.kind} ${o.view || ''}`, loading: 'lazy' })),
    el('figcaption', {}, [
      el('span', { class: `chip ${o.new ? 'queued' : 'ready'}`, 'data-badge': o.new ? 'new' : 'cached', text: o.new ? 'new' : 'cached' }), ' ',
      `${o.view || o.kind.replace('satellite_', 'sat ')}${o.station_s != null ? ` · ${Number(o.station_s).toFixed(0)} m` : ''}${o.capture_date ? ` · ${o.capture_date}` : ''}`,
    ]),
  ])));
}
