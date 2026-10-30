// Entities table with filters + evidence drawer (supporting images with the detection box drawn from crop_px).
// API: GET .../model/entities (list), GET .../model/entities/{id}?version= (drawer).

import { el, aget, segPath, pct } from '/js/admin/auth.js?v=4';

/** Box overlay as CSS percentages from crop_px [x, y, w, h] over the image size. */
export function boxStyle(crop, w, h) {
  if (!crop || !w || !h) return null;
  const [x, y, cw, ch] = crop;
  return `left:${(100 * x) / w}%;top:${(100 * y) / h}%;width:${(100 * cw) / w}%;height:${(100 * ch) / h}%`;
}

function evidenceFigure(ev, { large = false } = {}) {
  const ob = ev.observation || {};
  const style = boxStyle(ev.crop_px, ob.width_px, ob.height_px);
  return el('div', { class: `ev-img${large ? ' large' : ''}` }, [
    el('img', { src: ob.url, alt: `${ob.kind || 'photo'} ${ob.view || ''} at ${ob.station_s != null ? Number(ob.station_s).toFixed(0) + ' m' : '?'}`, loading: large ? 'eager' : 'lazy' }),
    style ? el('span', { class: 'det-box', 'data-testid': 'det-box', style }) : null,
  ]);
}

function sparkline(history) {
  const pts = (history || []).filter((h) => h.confidence != null);
  const W = 160, H = 36;
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('class', 'spark'); svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `Confidence by version: ${pts.map((p) => `v${p.version} ${pct(p.confidence)}`).join(', ')}`);
  if (!pts.length) return svg;
  const xs = (i) => (pts.length === 1 ? W / 2 : 6 + (i * (W - 12)) / (pts.length - 1));
  const ys = (c) => H - 4 - c * (H - 8);
  const line = document.createElementNS(ns, 'polyline');
  line.setAttribute('points', pts.map((p, i) => `${xs(i)},${ys(p.confidence)}`).join(' '));
  line.setAttribute('fill', 'none'); line.setAttribute('stroke', 'currentColor'); line.setAttribute('stroke-width', '2');
  svg.append(line);
  pts.forEach((p, i) => {
    const c = document.createElementNS(ns, 'circle');
    c.setAttribute('cx', xs(i)); c.setAttribute('cy', ys(p.confidence)); c.setAttribute('r', '3'); c.setAttribute('fill', 'currentColor');
    svg.append(c);
  });
  return svg;
}

export function openViewer(ev) {
  const prev = document.activeElement;
  const dlg = el('div', { class: 'viewer', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Full image', 'data-testid': 'image-viewer' });
  const close = el('button', { type: 'button', text: 'Close' });
  const onKey = (e) => { if (e.key === 'Escape') done(); };
  const done = () => { dlg.remove(); document.removeEventListener('keydown', onKey); if (prev && prev.focus) prev.focus(); };
  close.addEventListener('click', done);
  document.addEventListener('keydown', onKey);
  const ob = ev.observation || {};
  dlg.append(close, evidenceFigure(ev, { large: true }),
    el('p', { class: 'muted', text: `${ob.kind || ''} ${ob.view || ''} · station ${ob.station_s != null ? Number(ob.station_s).toFixed(1) : '?'} m · ${ob.capture_date || 'date unknown'}${ev.note ? ' · ' + ev.note : ''}` }));
  document.body.append(dlg);
  close.focus();
}

export function renderEvidence(drawer, detail) {
  const e = detail.entity;
  const ev = detail.evidence || [];
  const supports = ev.filter((x) => x.role !== 'contradicts');
  const contra = ev.filter((x) => x.role === 'contradicts');
  const attrs = Object.entries(e.attributes || {});
  const close = el('button', { type: 'button', class: 'secondary small', text: 'Close', onclick: () => { drawer.hidden = true; } });
  drawer.dataset.entityId = e.id;
  drawer.replaceChildren(
    el('div', { class: 'section-head' }, [el('h3', { text: `${e.type}${e.subtype ? ` · ${e.subtype}` : ''} at ${Number(e.position.s).toFixed(1)} m` }), close]),
    el('p', { class: e.state === 'removed' ? 'removed' : '', 'data-testid': 'evidence-description', text: e.description || '(no description)' }),
    el('p', { class: 'muted', text: `${e.state} · confidence ${pct(e.confidence)} · first seen v${e.first_seen_version}, last changed v${e.last_changed_version}` }),
    e.state === 'removed'
      ? el('p', { class: 'notice', 'data-testid': 'removed-reason', text: `Removed: ${contra.length ? `${contra.length} later photo${contra.length === 1 ? '' : 's'} should show it but do not${contra[0].note ? ` (${contra[0].note})` : ''}.` : 'confidence fell below the removal threshold.'} Kept for the record.` })
      : null,
    attrs.length ? el('dl', { class: 'kv' }, attrs.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: typeof v === 'object' ? JSON.stringify(v) : String(v) })])) : el('p', { class: 'muted', text: 'No attributes recorded.' }),
    el('div', { class: 'spark-row' }, [el('span', { class: 'muted', text: 'Confidence by version' }), sparkline(detail.history),
      el('span', { class: 'muted', text: (detail.history || []).map((h) => `v${h.version} ${pct(h.confidence)}`).join(' → ') })]),
    (detail.contradictions || []).length
      ? el('div', {}, [el('h4', { text: 'Contradictions' }), el('ul', {}, detail.contradictions.map((c) => el('li', { text: `${c.kind}: ${c.description}${c.resolved_in_version ? ` (resolved in v${c.resolved_in_version})` : ' (open)'}` })))])
      : el('p', { class: 'muted', text: 'No contradictions.' }),
    el('h4', { text: `Supporting images (${supports.length})` }),
    el('div', { class: 'thumbs', 'data-testid': 'evidence-thumbs' }, supports.length
      ? supports.map((x) => {
          const ob = x.observation || {};
          const b = el('button', { type: 'button', class: 'thumb', 'data-testid': 'evidence-thumb', title: 'Open the full image', onclick: () => openViewer(x) }, evidenceFigure(x));
          return el('div', { class: 'thumb-wrap' }, [b, el('small', { class: 'muted', text: `${ob.view || ob.kind || ''} · ${ob.station_s != null ? Number(ob.station_s).toFixed(0) + ' m' : ''} · weight ${Number(x.weight).toFixed(1)}` })]);
        })
      : [el('p', { class: 'muted', text: 'No supporting images recorded.' })]),
  );
  drawer.hidden = false;
}

export function createEntities({ root, slug, getVersion, onAuth }) {
  const tbody = root.querySelector('tbody');
  const drawer = root.querySelector('[data-testid=evidence-drawer]');
  const fType = root.querySelector('[name=f-type]');
  const fState = root.querySelector('[name=f-state]');
  const fConf = root.querySelector('[name=f-conf]');
  const count = root.querySelector('.ent-count');
  let all = [];

  function filtered() {
    const min = Number.parseFloat(fConf.value) || 0;
    return all.filter((e) => (!fType.value || e.type === fType.value) && (!fState.value || e.state === fState.value) && e.confidence >= min);
  }

  function draw() {
    const rows = filtered();
    count.textContent = `${rows.length} of ${all.length} entities`;
    tbody.replaceChildren(...rows.map((e) => {
      const tr = el('tr', { 'data-entity-id': e.id, 'data-state': e.state, class: e.state === 'removed' ? 'removed' : '', tabindex: '0', role: 'button', 'aria-label': `Show evidence for ${e.type} at ${Number(e.s).toFixed(0)} metres` }, [
        el('td', { text: e.subtype ? `${e.type} (${e.subtype})` : e.type }),
        el('td', { text: Number(e.s).toFixed(1) }),
        el('td', { text: e.t >= 0 ? 'left' : 'right' }),
        el('td', {}, el('div', { class: 'bar small conf', title: pct(e.confidence) }, el('div', { class: 'bar-fill', style: `width:${100 * e.confidence}%` }))),
        el('td', {}, el('span', { class: `chip ent-${e.state}`, text: e.state })),
        el('td', { class: 'desc', text: e.description }),
        el('td', { text: String(e.evidence_count) }),
      ]);
      const open = () => openEntity(e.id);
      tr.addEventListener('click', open);
      tr.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); } });
      return tr;
    }));
    root.querySelector('[data-role=empty-filter]').hidden = !(all.length && !rows.length);
  }

  async function openEntity(id) {
    drawer.hidden = false;
    drawer.replaceChildren(el('p', { class: 'muted', text: 'Loading evidence…' }));
    drawer.scrollIntoView({ block: 'nearest' });
    for (const tr of tbody.children) tr.setAttribute('aria-selected', String(tr.dataset.entityId === id));
    try {
      const v = getVersion();
      renderEvidence(drawer, await aget(segPath(slug, `/model/entities/${encodeURIComponent(id)}${v != null ? `?version=${v}` : ''}`)));
    } catch (err) {
      if (onAuth && onAuth(err)) return;
      drawer.replaceChildren(el('p', { class: 'error', role: 'alert', text: `Couldn't load evidence — ${err.message}` }),
        el('button', { type: 'button', class: 'secondary', text: 'Retry', onclick: () => openEntity(id) }));
    }
  }

  for (const c of [fType, fState, fConf]) c.addEventListener('input', draw);
  return {
    open: openEntity,
    set(list) {
      all = list;
      const types = [...new Set(list.map((e) => e.type))].sort();
      fType.replaceChildren(el('option', { value: '', text: 'All types' }), ...types.map((t) => el('option', { value: t, text: t })));
      draw();
    },
  };
}
