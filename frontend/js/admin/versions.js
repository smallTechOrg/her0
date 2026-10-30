// Version history (newest first) with two selectors + Compare. Data: GET .../model -> versions[] (ascending in the API).

import { el, fmtDate } from '/js/admin/auth.js?v=4';

const KIND_TEXT = { legacy: 'legacy import', prior: 'map prior', fused: 'fused', edit: 'edit' };

export function renderVersions(root, versions, { onCompare }) {
  const rows = [...versions].sort((a, b) => b.version - a.version);
  const tbody = root.querySelector('tbody');
  tbody.replaceChildren(...rows.map((v) => el('tr', { 'data-version': v.version }, [
    el('td', {}, el('b', { text: `v${v.version}` })),
    el('td', {}, el('span', { class: `chip kind-${v.kind}`, text: KIND_TEXT[v.kind] || v.kind })),
    el('td', { text: fmtDate(v.created_at) }),
    el('td', {}, v.run_id ? el('a', { href: `/admin/run.html?id=${encodeURIComponent(v.run_id)}`, text: 'run' }) : el('span', { class: 'muted', text: '—' })),
    el('td', { text: `${v.entity_count} entities` }),
    el('td', { text: typeof v.fidelity_level === 'number' ? `L${v.fidelity_level}` : (v.fidelity_level || '—') }),
    el('td', { class: 'desc', text: v.summary || '' }),
  ])));
  const from = root.querySelector('[name=cmp-from]');
  const to = root.querySelector('[name=cmp-to]');
  const btn = root.querySelector('[data-testid=compare]');
  const opts = () => rows.map((v) => el('option', { value: v.version, text: `v${v.version}` }));
  from.replaceChildren(...opts()); to.replaceChildren(...opts());
  const enough = rows.length >= 2;
  // the latest two are pre-selected: from = second newest, to = newest
  to.value = String(rows[0].version);
  from.value = String((rows[1] || rows[0]).version);
  btn.disabled = !enough;
  btn.title = enough ? 'Compare the two selected versions' : 'Only one version so far — nothing to compare yet.';
  root.querySelector('.cmp-note').textContent = enough ? '' : 'Only one version so far — a second version appears after the next Generate.';
  btn.onclick = () => onCompare(Number(from.value), Number(to.value));
  return enough ? { from: Number(from.value), to: Number(to.value) } : null;
}
