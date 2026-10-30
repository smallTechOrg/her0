// Results overlay: score + best status, walkability card, hazard map, detected objects, actions.
import { renderHazardMap } from '/js/game/overlay.js?v=4';
import { fmtDate } from '/js/game/about.js?v=4';

const LANE_NAMES = ['footpath', 'road edge', 'road'];
const STATE_LABELS = { intact: 'intact', broken: 'broken', missing: 'missing', blocked: 'blocked', encroached: 'encroached', unknown: 'not yet observed' };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function objectRow(o, crashedId) {
  const low = o.confidence < 0.6;
  const hazard = o.action !== 'none';
  const li = document.createElement('li');
  li.dataset.id = o.id;
  li.dataset.hazard = String(hazard);
  li.dataset.crashed = String(o.id === crashedId);
  if (low) li.classList.add('low-confidence');
  const tags = [
    `<span class="tag tag-${esc(o.action)}">${esc(o.action)}</span>`,
    `<span class="tag">${o.lanes.length ? o.lanes.map((i) => LANE_NAMES[i]).join(' / ') : 'off the lanes'}</span>`,
    `${Math.round(o.confidence * 100)}% confidence`,
    low ? '<span class="tag tag-low">low confidence</span>' : '',
    o.scenery ? '<span class="tag tag-low">scenery · not a hazard</span>' : '',
    o.source ? '<span class="tag">from street view</span>' : '',
    o.id === crashedId ? '<span class="tag tag-dodge">crashed here</span>' : '',
  ].filter(Boolean).join(' ');
  const evidence = o.evidence && o.evidence.summary ? o.evidence.summary : '';
  li.innerHTML = `<div><div class="obj-label">${esc(o.label)} <span class="muted">· ${esc(o.type.replaceAll('_', ' '))}</span></div>
    ${o.description ? `<div class="obj-desc" data-testid="obj-description">${esc(o.description)}</div>` : ''}
    ${evidence ? `<div class="obj-evidence muted" data-testid="obj-evidence">${esc(evidence)}</div>` : ''}
    <div class="obj-meta">${tags}</div></div>`;
  li.prepend(thumbnail(o));
  return li;
}

function placeholderThumb(text) {
  const div = document.createElement('div');
  div.className = 'obj-thumb placeholder';
  div.textContent = text;
  return div;
}

function thumbnail(o) {
  const url = o.billboard_url || (o.source && o.source.image_url);
  if (!url) return placeholderThumb('satellite');
  const img = document.createElement('img');
  img.className = 'obj-thumb';
  img.alt = '';
  img.loading = 'lazy';
  img.src = url;
  img.onerror = () => img.replaceWith(placeholderThumb('no image'));
  return img;
}

/**
 * @param {HTMLElement} root  the #results section
 * @param {object} args {scene, snap, post: {status:'saved'|'failed'|'pending', data, error}, onRunAgain}
 * @returns {{crashMarker: object|null}}
 */
export function renderResults(root, { scene, snap, post, onRunAgain, onAbout }) {
  const w = scene.walkability;
  const hazards = scene.objects.filter((o) => o.action !== 'none');
  const anyLow = hazards.some((o) => o.confidence < 0.6);
  const crashedId = snap.crashed_into ? snap.crashed_into.id : null;
  const crashedObj = scene.objects.find((o) => o.id === crashedId);
  const quality = scene.generation?.quality || 'unknown';

  let bestHtml = '';
  if (post.status === 'saved') {
    bestHtml = post.data.is_new_best
      ? '<span id="best-status" class="new-best">New best!</span>'
      : `<span id="best-status">Best: ${Number(post.data.best_score).toLocaleString('en-US')}</span>`;
  } else {
    bestHtml = '<span id="best-status" class="muted">Best: not saved</span>';
  }

  const v2 = scene.schema_version === 2;
  const about = v2 ? scene.about : null;
  const modelLine = v2
    ? (about.legacy ? 'Legacy scene · placement unverified'
      : `Model v${scene.model.version} · fidelity ${about.fidelity.level} · ${about.photos.total} photos · updated ${fmtDate(about.last_updated)}`)
    : 'Legacy scene · placement unverified';
  const uncertain = v2 && about.low_confidence_parts.length
    ? `Least certain: ${about.low_confidence_parts.map((p) => `${p.s_from}–${p.s_to} m (${p.reason})`).join('; ')}.` : '';
  const coverage = scene.cross_section.footpath_coverage;
  const stateList = coverage.map((c) =>
    `<li data-state="${esc(c.state)}">${Math.round(c.from_m)}–${Math.round(c.to_m)} m: ${esc(STATE_LABELS[c.state] || c.state)}</li>`).join('');

  root.replaceChildren(
    el(`<h2>${crashedObj ? `Out of health at ${esc(crashedObj.label)}` : 'Run over'}</h2>`),
    el(`<div class="score-line"><span class="score-big" id="result-score">${snap.score}</span><span>points</span>${bestHtml}</div>`),
    el(`<div class="stat-row"><span><b id="result-distance">${Math.round(snap.distance_m)}</b> m</span>
        <span><b id="result-tokens">${snap.tokens}</b> tokens</span><span>Loops <b id="result-loops">${snap.loops}</b></span>
        <span><b id="result-hits">${snap.hits ?? 0}</b> hits</span></div>`),
    el(`<div class="notice" id="save-notice" ${post.status === 'failed' ? '' : 'hidden'}>Score couldn't be saved — check your connection${post.error ? ` (${esc(post.error)})` : ''}</div>`),
    el(`<section class="card" aria-labelledby="walk-h"><h3 id="walk-h">Walkability — ${esc(scene.segment.name)}</h3>
        <dl class="walk-grid">
          <div><dt>Footpath coverage</dt><dd id="walk-coverage">${w.footpath_coverage_pct}%</dd><div class="bar"><span style="width:${Math.max(0, Math.min(100, w.footpath_coverage_pct))}%"></span></div></div>
          <div><dt>Hazards</dt><dd id="walk-hazards">${w.hazard_count}</dd></div>
          <div><dt>Crossing present</dt><dd id="walk-crossing">${w.crossing_present ? 'Yes' : 'No'}</dd></div>
          <div><dt>Walkability score</dt><dd id="walk-score">${w.score}/100</dd></div>
        </dl>
        <p class="walk-note"><span class="tag">${esc(quality.replace('_', '-'))}</span>${anyLow ? ' low-confidence detections included' : ''}</p>
      </section>`),
    el(`<section class="card" aria-labelledby="map-h"><h3 id="map-h">Hazard map</h3><div id="hazard-map"></div><p id="map-caption"></p>
        <ul class="legend"><li><i style="background:#4ea1ff"></i>jump</li><li><i style="background:#c07bff"></i>slide</li>
        <li><i style="background:#ff4d4f"></i>dodge</li><li><i style="background:#ffb100"></i>token</li><li><i style="border:2px solid #fff"></i>crashed</li></ul></section>`),
    el(`<section class="card" aria-labelledby="fp-h" data-testid="footpath-states"><h3 id="fp-h">Footpath along the street</h3><ul class="state-list">${stateList}</ul></section>`),
    el('<section class="card" aria-labelledby="det-h"><h3 id="det-h">Detected objects</h3><ul id="detected"></ul></section>'),
    el(`<section class="card" data-testid="results-model"><p id="results-model-line" data-testid="results-model-line">${esc(modelLine)}</p>
        ${uncertain ? `<p class="muted" data-testid="results-uncertain">${esc(uncertain)}</p>` : ''}
        <button type="button" class="btn-secondary" id="results-about-open" data-testid="about-open-results">About this street</button></section>`),
    el('<div class="buttons"><button type="button" id="run-again">Run again</button><a class="btn-secondary" id="back-gallery" href="/">Back to gallery</a></div>'),
  );
  root.querySelector('#detected').replaceChildren(...scene.objects.map((o) => objectRow(o, crashedId)));
  root.querySelector('#run-again').addEventListener('click', onRunAgain);
  const aboutBtn = root.querySelector('#results-about-open');
  aboutBtn.addEventListener('click', () => { if (onAbout) onAbout(aboutBtn); });
  root.hidden = false;
  root.scrollTop = 0;
  const map = renderHazardMap(root.querySelector('#hazard-map'), root.querySelector('#map-caption'), scene, crashedId);
  return { crashMarker: map.crashMarker };
}
