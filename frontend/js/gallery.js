// Gallery: one card per ready segment from GET /api/segments. States: loading / empty / error / populated.
import { api } from '/js/api.js';

const state = { phase: 'loading', segments: [], error: null };
window.__hero = { page: 'gallery', get state() { return structuredClone(state); } };

const $ = (id) => document.getElementById(id);
const els = { loading: $('gallery-loading'), empty: $('gallery-empty'), error: $('gallery-error'),
  reason: $('gallery-error-reason'), list: $('street-list'), retry: $('gallery-retry') };

function show(which) {
  for (const key of ['loading', 'empty', 'error', 'list']) els[key].hidden = key !== which;
}

function formatBest(seg) {
  if (seg.best_score === null || seg.best_score === undefined) return { text: 'No runs yet', none: true };
  return { text: `Best ${Number(seg.best_score).toLocaleString('en-US')}`, none: false };
}

function qualityLabel(quality) {
  if (quality === 'full') return null;
  if (quality === 'satellite_only') return 'satellite-only';
  return quality || 'unknown quality';
}

function card(seg) {
  const li = document.createElement('li');
  const article = document.createElement('article');
  article.className = 'street-card';
  article.dataset.slug = seg.slug;
  const link = document.createElement('a');
  link.href = `/play/?segment=${encodeURIComponent(seg.slug)}`;
  link.setAttribute('aria-label', `Run ${seg.name}`);

  if (seg.thumbnail_url) {
    const img = document.createElement('img');
    img.className = 'street-thumb';
    img.src = seg.thumbnail_url;
    img.alt = `Satellite view of ${seg.name} with hazard overlay`;
    img.loading = 'lazy';
    img.onerror = () => { img.replaceWith(thumbMissing()); };
    link.appendChild(img);
  } else {
    link.appendChild(thumbMissing());
  }

  const body = document.createElement('div');
  body.className = 'street-body';
  const h2 = document.createElement('h2');
  h2.textContent = seg.name;
  const meta = document.createElement('p');
  meta.className = 'street-meta';
  meta.textContent = `${seg.city} · ${Math.round(seg.length_m)} m`;
  const row = document.createElement('div');
  row.className = 'street-row';
  const score = document.createElement('span');
  score.className = 'badge badge-score';
  score.title = 'Walkability score';
  score.textContent = `${seg.walkability?.score ?? '–'}/100`;
  row.appendChild(score);
  const q = qualityLabel(seg.quality);
  if (q) {
    const tag = document.createElement('span');
    tag.className = 'badge badge-quality';
    tag.textContent = q;
    row.appendChild(tag);
  }
  const best = formatBest(seg);
  const bestEl = document.createElement('span');
  bestEl.className = 'best' + (best.none ? ' none' : '');
  bestEl.textContent = best.text;
  row.appendChild(bestEl);
  body.append(h2, meta, row);
  link.appendChild(body);
  article.appendChild(link);
  li.appendChild(article);
  return li;
}

function thumbMissing() {
  const div = document.createElement('div');
  div.className = 'street-thumb-missing';
  div.textContent = 'No thumbnail';
  return div;
}

async function load() {
  state.phase = 'loading';
  show('loading');
  try {
    const segments = await api.get('/api/segments');
    state.segments = segments;
    state.error = null;
    if (!segments.length) {
      state.phase = 'empty';
      show('empty');
      return;
    }
    els.list.replaceChildren(...segments.map(card));
    state.phase = 'populated';
    show('list');
  } catch (err) {
    state.phase = 'error';
    state.error = err.message;
    els.reason.textContent = err.message || 'retry';
    show('error');
  }
}

els.retry.addEventListener('click', load);
load();
