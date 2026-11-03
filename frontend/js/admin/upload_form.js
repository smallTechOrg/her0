// Real photo upload form: <input type=file multiple accept=image/*>, one editable row per file (position s, side, facing,
// date taken), a placement map to click the position on, then sequential POST .../observations/upload (one file each).
// API: POST /api/admin/segments/{slug}/observations/upload (multipart); POST .../generate for "Generate now".

import { getAdminPassword } from '/js/api.js?v=3';
import { el, apost, segPath } from '/js/admin/auth.js?v=5';
import { createPlacementMap, compassSide } from '/js/admin/placement_map.js?v=1';

const FACINGS = [
  ['forward', 'along the street ahead'], ['back', 'back along the street'], ['left', 'the left-hand buildings'],
  ['right', 'the right-hand buildings'], ['custom', 'custom heading…'],
];
const SIDES = ['left', 'right', 'centre'];

export function monthOf(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime()) || d.getTime() > Date.now() || d.getUTCFullYear() < 2000) return '';
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
}

const fmtSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

/**
 * @param {object} o
 *  root: the [data-testid=upload-form] element (already containing the static scaffold from segment.html)
 *  slug, getCtx(): {length, points, bins, observations, maxMb, accepts}, onUploaded(): Promise, onAuth(err): bool,
 *  activeReason(): string, mountModel(container): void, getModel(): string|null
 */
export function createUploadForm(o) {
  const { root, slug } = o;
  const input = root.querySelector('[data-testid=upload-file]');
  const drop = root.querySelector('.drop-zone');
  const list = root.querySelector('.upload-rows');
  const submit = root.querySelector('[data-testid=upload-submit]');
  const summary = root.querySelector('.upload-summary');
  const after = root.querySelector('.upload-after');
  const mapBox = root.querySelector('[data-testid=upload-map]');
  const emptyNote = root.querySelector('.upload-empty');
  let rows = [];
  let selected = null;
  let uploading = false;
  let seq = 0;
  let presetPos = null; // set by "Upload here" before any file is chosen

  const ctx = () => o.getCtx();
  const length = () => ctx().length || 100;

  const map = createPlacementMap(mapBox, {
    onPick: (s, side) => { if (selected) { setPos(selected, s, side); } },
    onNudge: (d) => { if (selected && selected.s != null) setPos(selected, selected.s + d, null); },
    onCell: (s) => { if (selected) setPos(selected, s, null); },
  });

  function clampS(s) { return Math.max(0, Math.min(length(), Math.round(s * 10) / 10)); }

  function setPos(row, s, side) {
    row.s = clampS(s);
    if (side) row.side = side;
    row.sync();
    // move the selection on to the next photo that still needs a position
    refreshState();
  }

  function sideLabel(side) {
    const b = map.bearing();
    return b == null ? `${side} of travel` : (side === 'centre' ? 'centre of the road' : compassSide(b, side));
  }

  function valid(r) { return r.status === 'queued' && r.s != null && r.side && (r.facing !== 'custom' || Number.isFinite(r.heading)); }

  function refreshState() {
    const pending = rows.filter((r) => r.status === 'queued');
    const bad = pending.filter((r) => !valid(r));
    const n = pending.length;
    submit.textContent = n ? `Upload ${n} photo${n === 1 ? '' : 's'}` : 'Upload photos';
    submit.disabled = uploading || !n || bad.length > 0;
    submit.title = !n ? 'Choose photos first' : bad.length ? 'Give every photo a position (click the map), a side, and a heading' : '';
    emptyNote.hidden = rows.length > 0;
    summary.textContent = bad.length ? `${bad.length} photo${bad.length === 1 ? '' : 's'} still need a position — click the map.` : '';
    map.setMarkers(rows.filter((r) => r.status !== 'removed').map((r, i) => ({
      id: r.id, s: r.s, side: r.side, selected: r === selected, label: String(i + 1),
    })));
    for (const r of rows) r.node.classList.toggle('selected', r === selected);
  }

  function select(row) { selected = row; refreshState(); }

  function addFile(file) {
    const max = ctx().maxMb || 10;
    const row = { id: ++seq, file, s: null, side: 'left', facing: 'forward', heading: NaN, date: monthOf(file.lastModified), status: 'queued', msg: '' };
    const url = URL.createObjectURL(file);
    row.url = url;
    const hint = el('p', { class: 'muted small-print row-hint', text: 'click the map to set the position' });
    const sIn = el('input', { name: 's', type: 'number', inputmode: 'decimal', min: '0', step: '0.1', 'aria-label': `Position along the street in metres for ${file.name}` });
    const sideSel = el('select', { name: 'side', 'aria-label': `Side of the road for ${file.name}` });
    const facingSel = el('select', { name: 'facing', 'aria-label': `Facing for ${file.name}` },
      FACINGS.map(([v, t]) => el('option', { value: v, text: t })));
    const headIn = el('input', { name: 'heading_deg', type: 'number', inputmode: 'decimal', min: '0', max: '360', step: '1', placeholder: 'compass °', hidden: true,
      'aria-label': `Compass heading in degrees for ${file.name}` });
    const dateIn = el('input', { name: 'captured_at', type: 'month', value: row.date, max: monthOf(Date.now()), 'aria-label': `Month taken for ${file.name}` });
    const status = el('p', { class: 'row-status', role: 'status', 'data-testid': 'upload-row-status' });
    const rm = el('button', { type: 'button', class: 'secondary small', text: 'Remove', 'aria-label': `Remove ${file.name}` });
    const tooBig = file.size > max * 1048576;
    const bad = !/^image\/(jpeg|png)$/.test(file.type || '');
    const node = el('li', { class: 'upload-row', 'data-testid': 'upload-row', 'data-name': file.name, tabindex: '0' }, [
      el('img', { class: 'thumb', src: url, alt: `Preview of ${file.name}` }),
      el('div', { class: 'row-body' }, [
        el('p', { class: 'row-title' }, [el('strong', { text: file.name }), ` · ${fmtSize(file.size)}`]),
        el('div', { class: 'row-fields' }, [
          el('label', {}, ['Position s (m)', sIn]),
          el('label', {}, ['Side of the road', sideSel]),
          el('label', {}, ['Facing', facingSel]),
          el('label', { hidden: true, class: 'heading-label' }, ['Heading (°)', headIn]),
          el('label', {}, ['Date taken', dateIn]),
        ]),
        hint, status,
      ]),
      rm,
    ]);
    row.node = node;
    const headLabel = node.querySelector('.heading-label');
    row.sync = () => {
      sIn.value = row.s == null ? '' : String(row.s);
      sideSel.value = row.side;
      hint.hidden = row.s != null;
      const opts = [...sideSel.options];
      opts.forEach((op) => { op.textContent = op.value === 'centre' ? 'centre of the road' : sideLabel(op.value); });
    };
    row.setStatus = (kind, text) => {
      row.status = kind; row.msg = text;
      node.dataset.status = kind;
      status.textContent = text;
      status.className = `row-status ${kind}`;
      for (const f of node.querySelectorAll('input, select')) f.disabled = kind !== 'queued';
      rm.disabled = kind === 'uploading';
      refreshState();
    };
    for (const sd of SIDES) sideSel.append(el('option', { value: sd, text: sd }));
    sIn.addEventListener('input', () => { row.s = sIn.value === '' ? null : clampS(Number.parseFloat(sIn.value)); hint.hidden = row.s != null; refreshState(); });
    sideSel.addEventListener('change', () => { row.side = sideSel.value; refreshState(); });
    facingSel.addEventListener('change', () => {
      row.facing = facingSel.value; headLabel.hidden = headIn.hidden = row.facing !== 'custom'; refreshState();
    });
    headIn.addEventListener('input', () => { row.heading = Number.parseFloat(headIn.value); refreshState(); });
    dateIn.addEventListener('input', () => { row.date = dateIn.value; });
    node.addEventListener('focusin', () => { if (selected !== row) select(row); });
    node.addEventListener('click', () => { if (selected !== row) select(row); });
    rm.addEventListener('click', () => {
      row.status = 'removed'; URL.revokeObjectURL(url); node.remove(); rows = rows.filter((r) => r !== row);
      if (selected === row) selected = rows.find((r) => r.status === 'queued') || null;
      refreshState();
    });
    list.append(node);
    rows.push(row);
    row.sync();
    if (bad) row.setStatus('error', `${file.name} is not a JPEG or PNG — it won't be uploaded.`);
    else if (tooBig) row.setStatus('error', `${file.name} is ${fmtSize(file.size)}; the limit is ${max} MB.`);
    else row.setStatus('queued', 'Ready — set the position.');
    return row;
  }

  function addFiles(files) {
    const added = [...files].map(addFile);
    const first = added.find((r) => r.status === 'queued');
    if (first && presetPos != null) { first.s = clampS(presetPos); first.sync(); presetPos = null; }
    selected = (selected && selected.status === 'queued') ? selected : first || selected;
    refreshState();
  }

  input.addEventListener('change', () => { addFiles(input.files); input.value = ''; });
  for (const evn of ['dragenter', 'dragover']) drop.addEventListener(evn, (e) => { e.preventDefault(); drop.classList.add('over'); });
  for (const evn of ['dragleave', 'drop']) drop.addEventListener(evn, (e) => { e.preventDefault(); drop.classList.remove('over'); });
  drop.addEventListener('drop', (e) => { if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files); });

  async function postOne(row) {
    const fd = new FormData();
    fd.append('file', row.file, row.file.name);
    fd.append('s', String(row.s));
    fd.append('side', row.side);
    if (row.facing === 'custom') fd.append('heading_deg', String(row.heading));
    else fd.append('facing', row.facing);
    if (row.date) fd.append('captured_at', row.date);
    const resp = await fetch(segPath(slug, '/observations/upload'), { method: 'POST', headers: { 'X-Hero-Admin-Password': getAdminPassword(), Accept: 'application/json' }, body: fd });
    let payload = null;
    try { payload = await resp.json(); } catch { /* handled below */ }
    if (resp.status === 401) throw Object.assign(new Error('Session expired'), { status: 401 });
    if (!payload || !payload.ok) throw new Error((payload && payload.error && payload.error.message) || `Upload failed (${resp.status}).`);
    return { data: payload.data, status: resp.status };
  }

  submit.addEventListener('click', async () => {
    const todo = rows.filter(valid);
    if (!todo.length) return;
    uploading = true; refreshState();
    for (const r of todo) r.setStatus('queued', 'Queued…');
    let done = 0;
    for (const r of todo) {
      r.setStatus('uploading', 'Uploading…');
      try {
        const { data, status } = await postOne(r);
        const a = data.affects || {};
        const lift = a.can_reach_level ? ` · can lift ${Number(a.s_from).toFixed(0)}–${Number(a.s_to).toFixed(0)} m to L${a.can_reach_level}` : '';
        r.setStatus('done', `${status === 200 ? 'Already uploaded — ' : ''}placed at ${Number((data.placement || {}).s ?? r.s).toFixed(0)} m${lift}`);
        done += 1;
      } catch (err) {
        if (o.onAuth && o.onAuth(err)) { uploading = false; return; }
        r.setStatus('error', err.message);
      }
    }
    uploading = false;
    refreshState();
    if (done) {
      after.hidden = false;
      after.querySelector('.after-text').textContent = `${done} photo${done === 1 ? '' : 's'} uploaded — not yet fused into the model.`;
      try { await o.onUploaded(); } catch { /* the grid shows its own error */ }
    }
  });

  const gen = after.querySelector('[data-testid=upload-generate]');
  const genMsg = after.querySelector('.form-msg');
  gen.addEventListener('click', async () => {
    gen.disabled = true; genMsg.hidden = true;
    try {
      const body = {};
      const m = o.getModel && o.getModel();
      if (m) body.model = m;
      const data = await apost(segPath(slug, '/generate'), body);
      location.href = `/admin/run.html?id=${encodeURIComponent(data.run_id)}`;
    } catch (err) {
      if (o.onAuth && o.onAuth(err)) return;
      genMsg.className = 'form-msg notice error'; genMsg.textContent = `Couldn't start the run — ${err.message}`; genMsg.hidden = false; gen.disabled = false;
    }
  });
  after.querySelector('[data-testid=upload-later]').addEventListener('click', () => { after.hidden = true; });
  if (o.mountModel) o.mountModel(after.querySelector('.model-slot'));

  return {
    /** Refresh the map after the model/observations changed. */
    refresh() {
      const c = ctx();
      map.setData({ points: c.points, length: c.length, observations: c.observations, bins: c.bins });
      for (const r of rows) r.sync();
      refreshState();
    },
    /** "Upload here" from a weak interval: pre-set the position of the selected (or next unplaced) photo. */
    presetS(s) {
      const target = selected || rows.find((r) => r.status === 'queued');
      root.scrollIntoView({ block: 'start' });
      if (target) { select(target); setPos(target, s, null); } else { presetPos = s; summary.textContent = `Choose photos: the first one will be placed near ${Math.round(s)} m.`; }
    },
    get rows() { return rows; },
  };
}