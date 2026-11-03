// Fetch wrapper for the #hero API envelope ({ok, data} | {ok:false, error:{code,message,detail}}).
// Scaffold-owned; slices import it, they do not edit it.

const ADMIN_HEADER = 'X-Hero-Admin-Password';
const STORAGE_KEY = 'hero.adminPassword';

export class ApiError extends Error {
  constructor(code, message, detail, status) {
    super(message);
    this.code = code;
    this.detail = detail;
    this.status = status;
  }
}

export function getAdminPassword() {
  return sessionStorage.getItem(STORAGE_KEY) || '';
}

export function setAdminPassword(password) {
  if (password) sessionStorage.setItem(STORAGE_KEY, password);
  else sessionStorage.removeItem(STORAGE_KEY);
}

async function request(method, path, body, { admin = false } = {}) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (admin) headers[ADMIN_HEADER] = getAdminPassword();
  let resp;
  try {
    resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (err) {
    throw new ApiError('network_error', 'Could not reach the server.', String(err), 0);
  }
  let payload;
  try {
    payload = await resp.json();
  } catch {
    throw new ApiError('internal_error', `Unexpected response (${resp.status}).`, null, resp.status);
  }
  if (!payload.ok) {
    const e = payload.error || {};
    throw new ApiError(e.code || 'internal_error', e.message || 'Request failed.', e.detail, resp.status);
  }
  return payload.data;
}

export const api = {
  get: (path, opts) => request('GET', path, undefined, opts),
  post: (path, body, opts) => request('POST', path, body, opts),
  put: (path, body, opts) => request('PUT', path, body, opts),
  del: (path, opts) => request('DELETE', path, undefined, opts),
};
