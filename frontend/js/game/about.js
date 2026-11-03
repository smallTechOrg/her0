// "About this street" panel: plain-language account of what the model knows and how well (spec/ui.md).
// Reachable from the Ready overlay, the HUD pause icon and Results. Traps focus; closes with Esc / Close.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const LEVELS = [
  ['L0', 'map-only sketch'], ['L1', 'seen from one place'], ['L2', 'multi-view'], ['L3', 'close-up detail'],
];
const FOCUSABLE = 'button, a[href], [tabindex]:not([tabindex="-1"])';

export function fmtDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}` : (iso || '');
}

const metres = (n) => `${Math.round(n * 10) / 10}`.replace(/\.0$/, '');

function v1Photos(scene) {
  const urls = new Set(scene.objects.map((o) => o.source && o.source.image_url).filter(Boolean));
  return urls.size;
}

/** The panel body (HTML string) for a scene of either schema. */
export function aboutHtml(scene, manifest = null) {
  if (scene.schema_version !== 2 && scene.schema_version !== 3) {
    const n = v1Photos(scene);
    return `<p data-testid="about-summary">This street was built with the first-generation model (${n} photo${n === 1 ? '' : 's'}); its position was not verified.</p>
      <p class="about-warning" data-testid="about-warning">Legacy scene, placement unverified</p>`;
  }
  const a = scene.about;
  const hist = a.fidelity.histogram_m || {};
  const total = LEVELS.reduce((t, [k]) => t + (hist[k] || 0), 0) || 1;
  const segs = LEVELS.filter(([k]) => (hist[k] || 0) > 0)
    .map(([k]) => `<span data-level="${k}" style="width:${((hist[k] / total) * 100).toFixed(2)}%" title="${k} · ${metres(hist[k])} m"></span>`).join('');
  const legend = LEVELS.map(([k, name]) => `<li><i data-level="${k}"></i>${k} ${esc(name)}${hist[k] ? ` · ${metres(hist[k])} m` : ''}</li>`).join('');
  const dates = a.capture_dates && a.capture_dates.oldest
    ? ` · captured ${esc(a.capture_dates.oldest)}${a.capture_dates.newest && a.capture_dates.newest !== a.capture_dates.oldest ? ` to ${esc(a.capture_dates.newest)}` : ''}` : '';
  const low = a.low_confidence_parts || [];
  const lowHtml = low.length
    ? `<ul data-testid="about-least-certain">${low.map((p) => `<li>${metres(p.s_from)}–${metres(p.s_to)} m: ${esc(p.reason)}</li>`).join('')}</ul>`
    : '<p class="muted" data-testid="about-least-certain">Nothing flagged: every part meets the confidence bar.</p>';
  const warnings = [];
  if (a.legacy) warnings.push('Legacy scene, placement unverified');
  else if (!a.placement_verified) warnings.push('Placement not verified');
  if (a.degraded) warnings.push('Map-only sketch: no street photos yet');
  return `<p data-testid="about-summary">${esc(a.summary)}</p>
    ${warnings.map((w) => `<p class="about-warning" data-testid="about-warning">${esc(w)}</p>`).join('')}
    <div class="fid-bar" role="img" data-testid="about-fidelity-bar" aria-label="Fidelity along the street: ${esc(LEVELS.map(([k]) => `${k} ${metres(hist[k] || 0)} m`).join(', '))}">${segs}</div>
    <ul class="fid-legend">${legend}</ul>
    <p data-testid="about-photos">Photos: ${a.photos.street_view} Street View, ${a.photos.satellite} satellite, ${a.photos.user} uploaded${dates} · last updated ${esc(fmtDate(a.last_updated))}</p>
    <h3>Least certain</h3>${lowHtml}
    ${a.improve_hint ? `<p data-testid="about-improve">${esc(a.improve_hint)}</p>` : ''}
    ${v3Sections(scene, manifest)}`;
}

// ---- v3 (glb) additions. Low-fidelity labelling lives HERE only, never in the 3D world.
function interpolatedHtml(scene, manifest) {
  const parts = (manifest && manifest.interpolated && manifest.interpolated.length ? manifest.interpolated : scene.about.interpolated) || [];
  if (!parts.length) return '<p class="muted" data-testid="about-interpolated">Every stretch is backed by evidence; nothing was filled in.</p>';
  const rows = parts.map((p) => `<li data-what="${esc(p.what)}" data-side="${esc(p.side)}">${esc(String(p.what || '').replaceAll('_', ' '))}${p.side && p.side !== 'both' ? `, ${esc(p.side)} side` : ''} ${metres(p.s_from)}–${metres(p.s_to)} m: ${esc(p.note || p.method)}</li>`);
  return `<ul data-testid="about-interpolated">${rows.join('')}</ul>`;
}

function lowObjectsHtml(manifest) {
  const low = ((manifest && manifest.entities) || []).filter((e) => e.confidence < 0.6);
  if (!low.length) return '';
  return `<h3>Low-confidence objects</h3><ul data-testid="about-low-objects">${low.map((e) =>
    `<li data-id="${esc(e.id)}">${esc(String(e.type).replaceAll('_', ' '))} at ${metres(e.s)} m · ${Math.round(e.confidence * 100)}% confidence</li>`).join('')}</ul>`;
}

function v3Sections(scene, manifest) {
  if (scene.schema_version !== 3) return '';
  const a = scene.about;
  const attribution = manifest && manifest.attribution ? manifest.attribution.lines : [];
  return `<h3>Filled in from neighbouring stretches</h3>
    ${a.gap_summary ? `<p data-testid="about-gap-summary">${esc(a.gap_summary)}</p>` : ''}
    ${interpolatedHtml(scene, manifest)}
    ${lowObjectsHtml(manifest)}
    ${a.dynamic_summary ? `<h3>Spawned dynamically</h3><p data-testid="about-dynamic">${esc(a.dynamic_summary)}</p>` : ''}
    ${attribution.length ? `<p class="muted about-attribution" data-testid="about-attribution">${attribution.map(esc).join(' · ')}</p>` : ''}`;
}

/**
 * @param {HTMLElement} root  the #about-panel section
 * @param {object} scene
 * @param {{onOpen?:Function,onClose?:Function}} hooks
 */
export function createAbout(root, scene, { onOpen, onClose, manifest = null } = {}) {
  let opener = null;
  root.innerHTML = `<div class="about-card"><h2 id="about-title">About this street</h2>
    <div id="about-body">${aboutHtml(scene, manifest)}</div>
    <button type="button" id="about-close" data-testid="about-close">Close</button></div>`;
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-labelledby', 'about-title');

  const onKey = (ev) => {
    if (ev.key === 'Escape') { ev.preventDefault(); close(); return; }
    if (ev.key === 'Tab') {
      const items = [...root.querySelectorAll(FOCUSABLE)].filter((el) => !el.disabled);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
      else if (!root.contains(document.activeElement)) { ev.preventDefault(); first.focus(); }
    } else if (ev.key.startsWith('Arrow') || ev.key === ' ') {
      ev.stopPropagation(); // never reach the game controls while the dialog is open
    }
  };

  function open(from) {
    if (!root.hidden) return;
    opener = from || document.activeElement;
    root.hidden = false;
    root.scrollTop = 0;
    document.addEventListener('keydown', onKey, true);
    if (onOpen) onOpen();
    root.querySelector('#about-close').focus();
  }

  function close() {
    if (root.hidden) return;
    root.hidden = true;
    document.removeEventListener('keydown', onKey, true);
    if (opener && typeof opener.focus === 'function' && document.contains(opener)) opener.focus();
    if (onClose) onClose();
  }

  root.querySelector('#about-close').addEventListener('click', close);
  return { open, close, get isOpen() { return !root.hidden; } };
}
