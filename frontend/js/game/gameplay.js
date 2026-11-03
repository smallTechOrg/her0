// Gameplay rules as data (spec/portability.md section 5). Nothing here (or in any other game module) hard-codes a
// number from gameplay.json: it is read from manifest.gameplay, or from GET /api/gameplay.json for legacy scenes.

let current = null; // the last gameplay object loaded; a Runner built without an explicit `gameplay` falls back to it

/** Legacy-style constants object (read-only view derived from a gameplay object); filled in place by setCurrent(). */
export const C = {};

function fillC(gp) {
  for (const k of Object.keys(C)) delete C[k];
  const add = (obj) => {
    for (const [k, v] of Object.entries(obj)) {
      if (typeof v === 'number') C[k.toUpperCase().replace(/_MPS$/, '')] = v;
    }
  };
  add(gp.runner);
  for (const [k, v] of Object.entries(gp.tokens)) if (typeof v === 'number') C[`TOKEN_${k.toUpperCase()}`] = v;
}

function setCurrent(gp) {
  current = gp;
  fillC(gp);
  return gp;
}

/** manifest.gameplay when a manifest is given, else GET /api/gameplay.json. Resolves to the gameplay object. */
export async function loadGameplay(manifest = null) {
  if (manifest && manifest.gameplay) return setCurrent(manifest.gameplay);
  const res = await fetch('/api/gameplay.json', { headers: { accept: 'application/json' } });
  if (!res.ok) throw new Error(`gameplay.json: HTTP ${res.status}`);
  const body = await res.json();
  return setCurrent(body && body.schema ? body : (body.data || body));
}

/** The gameplay loaded so far (or null). */
export function currentGameplay() { return current; }
