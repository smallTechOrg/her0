// Admin password gate shared by /admin/ and /admin/run.html.
// Password lives in sessionStorage (via api.js); validated with GET /api/admin/status.

import { api, getAdminPassword, setAdminPassword, ApiError } from '/js/api.js?v=3';

export const ADMIN_STATUS_PATH = '/api/admin/status';

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function fmtUsd(n) {
  return `$${Number(n || 0).toFixed(2)}`;
}

export function fmtUsdPrecise(n) {
  return `$${Number(n || 0).toFixed(4)}`;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return '—';
  if (ms >= 60000) return `${Math.floor(ms / 60000)}m ${String(Math.round((ms % 60000) / 1000)).padStart(2, '0')}s`;
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

export function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

export const pct = (x) => `${Math.round(100 * Number(x || 0))}%`;
export const m1 = (x) => `${Number(x).toFixed(Number.isInteger(Number(x)) ? 0 : 1)}`;

/** Admin GET/POST helpers with the password header. */
export const aget = (path) => api.get(path, { admin: true });
export const apost = (path, body) => api.post(path, body, { admin: true });
export const segPath = (slug, rest = '') => `/api/admin/segments/${encodeURIComponent(slug)}${rest}`;

/** Image URL as served: absolute path under /media, passed through untouched. */
export const mediaUrl = (u) => u || '';

export function setState(root, state) {
  root.dataset.current = state;
  for (const panel of root.querySelectorAll('[data-state]')) {
    panel.hidden = panel.dataset.state !== state;
  }
}

export async function fetchStatus() {
  return api.get(ADMIN_STATUS_PATH, { admin: true });
}

/**
 * Gate the page behind the admin password.
 * Shows #login (with inline "Wrong password") until GET /api/admin/status succeeds, then
 * calls onReady(status). Returns nothing; failures are rendered, never thrown.
 */
export function gate({ onReady }) {
  const login = document.getElementById('login');
  const form = login.querySelector('form');
  const input = form.querySelector('input[type=password]');
  const msg = login.querySelector('.login-msg');
  const button = form.querySelector('button[type=submit]');

  const show = (stateName, text = '') => {
    login.hidden = false;
    login.dataset.state = stateName; // login | unauthorized | error
    msg.textContent = text;
    msg.hidden = !text;
  };

  async function attempt(password, { fromForm }) {
    setAdminPassword(password);
    button.disabled = true;
    button.textContent = 'Checking…';
    try {
      const status = await fetchStatus();
      login.hidden = true;
      await onReady(status);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAdminPassword('');
        show('unauthorized', fromForm ? 'Wrong password' : 'Session expired — enter the admin password again.');
        input.focus();
      } else {
        show('error', `Couldn't reach the server: ${err.message}`);
      }
    } finally {
      button.disabled = false;
      button.textContent = 'Enter';
    }
  }

  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const pw = input.value.trim();
    if (!pw) { show('error', 'Enter the admin password.'); return; }
    attempt(pw, { fromForm: true });
  });

  const existing = getAdminPassword();
  if (existing) attempt(existing, { fromForm: false });
  else { show('login'); input.focus(); }
}

export function logout() {
  setAdminPassword('');
  location.reload();
}
