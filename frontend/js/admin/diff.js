// Version diff panel. API: GET .../model/diff?from=&to= -> ModelDiff (spec/data.md "Model diff").

import { el, pct } from '/js/admin/auth.js?v=4';

const fmtVal = (v) => (Array.isArray(v) ? v.map(fmtVal).join(', ').replace(/^/, '(').replace(/$/, ')') : v && typeof v === 'object' ? JSON.stringify(v) : v == null ? '—' : typeof v === 'number' ? String(Math.round(v * 100) / 100) : String(v));

function fieldLines(fields) {
  const out = [];
  for (const [k, v] of Object.entries(fields || {})) {
    if (k === 'attributes' && v && !Array.isArray(v)) {
      for (const [ak, av] of Object.entries(v)) out.push(`${ak}: ${fmtVal(av[0])} → ${fmtVal(av[1])}`);
    } else if (Array.isArray(v) && v.length === 2) out.push(`${k}: ${fmtVal(v[0])} → ${fmtVal(v[1])}`);
    else out.push(`${k}: ${fmtVal(v)}`);
  }
  return out;
}

const section = (title, items, testid) => el('div', { class: 'diff-sec', 'data-testid': testid }, [
  el('h4', { text: `${title} (${items.length})` }),
  items.length ? el('ul', {}, items) : el('p', { class: 'muted', text: 'None.' }),
]);

export function renderDiff(root, d, { onOpenEntity } = {}) {
  const body = root.querySelector('.diff-body');
  const ent = d.entities || {};
  const link = (id, text) => el('a', { href: `#entities`, onclick: (e) => { if (onOpenEntity) { e.preventDefault(); onOpenEntity(id); } }, text });
  const lines = d.summary_lines || [];
  const fid = (d.fidelity && d.fidelity.improved) || [];
  const prof = (d.profile && d.profile.changed_intervals) || [];
  const ctr = d.contradictions || {};
  body.replaceChildren(
    el('h3', { 'data-testid': 'diff-title', text: `v${d.from_version} → v${d.to_version}${d.is_empty ? ' · no differences' : ''}` }),
    el('ul', { class: 'diff-summary', 'data-testid': 'diff-summary' }, lines.length ? lines.map((l) => el('li', { text: l })) : [el('li', { class: 'muted', text: 'Nothing changed between these versions.' })]),
    section('Added entities', (ent.added || []).map((a) => el('li', {}, [link(a.id, `${a.type} at ${Number(a.s).toFixed(1)} m`), ` · ${pct(a.confidence)} · ${a.description || ''}`])), 'diff-added'),
    section('Removed entities', (ent.removed || []).map((a) => el('li', {}, [link(a.id, `${a.type} at ${Number(a.s).toFixed(1)} m`), ` — ${a.reason || ''}`])), 'diff-removed'),
    section('Revived entities', (ent.revived || []).map((a) => el('li', {}, [link(a.id, `${a.type} at ${Number(a.s).toFixed(1)} m`), ` · ${a.description || ''}`])), 'diff-revived'),
    section('Changed entities', (ent.changed || []).map((c) => el('li', {}, [link(c.id, c.id), c.reason ? ` — ${c.reason}` : '', el('ul', {}, fieldLines(c.fields).map((t) => el('li', { text: t })))])), 'diff-changed'),
    section('Profile changes', prof.map((p) => el('li', { text: `${Number(p.s_from).toFixed(0)}–${Number(p.s_to).toFixed(0)} m: ${p.what}` })), 'diff-profile'),
    section('Fidelity improvements', fid.map((f) => el('li', { class: 'fid-line', text: `${f.from} → ${f.to} over ${Number(f.s_from).toFixed(0)}–${Number(f.s_to).toFixed(0)} m` })), 'diff-fidelity'),
    section('Contradictions opened', (ctr.opened || []).map((c) => el('li', { text: c.description || c.id })), 'diff-opened'),
    section('Contradictions resolved', (ctr.resolved || []).map((c) => el('li', { text: c.description || c.id })), 'diff-resolved'),
    d.buildings && ((d.buildings.added || []).length + (d.buildings.changed || []).length + (d.buildings.removed || []).length)
      ? el('p', { class: 'muted', text: `Buildings: ${d.buildings.added.length} added, ${d.buildings.changed.length} changed, ${d.buildings.removed.length} removed.` }) : null,
  );
}
