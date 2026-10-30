// Hazard map: the satellite tile on a canvas with the segment line, hazard markers at tile_px
// (coloured by action), token markers and a highlight on the crashed-into object. Tap → label + confidence.
const COLORS = { jump: '#4ea1ff', slide: '#c07bff', dodge: '#ff4d4f', none: '#d9d9d9', token: '#ffb100' };
const HIT_RADIUS_CSS_PX = 16;

function framePx(ground, xM, latM) {
  const f = ground.frame_to_px;
  return [f.origin_px[0] + xM * f.along_px_per_m[0] + latM * f.across_px_per_m[0],
    f.origin_px[1] + xM * f.along_px_per_m[1] + latM * f.across_px_per_m[1]];
}

// Returns {canvas, markers:[{id,label,confidence,x,y,kind}], crashMarker} ; positions in CSS px of the canvas.
export function renderHazardMap(container, captionEl, scene, crashedId) {
  const [tileW, tileH] = scene.ground.size_px;
  const cssW = Math.max(200, Math.min(container.clientWidth || 360, 448));
  const scale = cssW / tileW;
  const cssH = Math.round(tileH * scale);
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  canvas.style.width = `${cssW}px`;
  canvas.style.height = `${cssH}px`;
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'Satellite hazard map');
  const ctx = canvas.getContext('2d');

  const markers = [];
  for (const o of scene.objects) {
    markers.push({ id: o.id, label: o.label, confidence: o.confidence, kind: o.action, type: o.type,
      x: o.tile_px[0] * scale, y: o.tile_px[1] * scale, crashed: o.id === crashedId });
  }
  for (const t of scene.tokens) {
    markers.push({ id: t.id, label: `token (${t.reason.replace('_', ' ')})`, confidence: null, kind: 'token',
      x: t.tile_px[0] * scale, y: t.tile_px[1] * scale, crashed: false });
  }

  const draw = (img) => {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, cssW, cssH);
    if (img) ctx.drawImage(img, 0, 0, cssW, cssH);
    // segment line start → end along the centreline
    const [sx, sy] = framePx(scene.ground, 0, 0);
    const [ex, ey] = framePx(scene.ground, scene.segment.length_m, 0);
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(sx * scale, sy * scale);
    ctx.lineTo(ex * scale, ey * scale);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 11px system-ui, sans-serif';
    ctx.fillText('start', sx * scale + 6, sy * scale + 4);
    for (const m of markers) {
      const r = m.kind === 'token' ? 4 : 6;
      if (m.crashed) {
        ctx.beginPath();
        ctx.arc(m.x, m.y, r + 6, 0, Math.PI * 2);
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 3;
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.arc(m.x, m.y, r, 0, Math.PI * 2);
      ctx.fillStyle = COLORS[m.kind] || '#fff';
      ctx.fill();
      ctx.strokeStyle = m.confidence !== null && m.confidence < 0.6 ? '#9aa0a6' : '#000';
      ctx.lineWidth = m.confidence !== null && m.confidence < 0.6 ? 2 : 1;
      ctx.stroke();
    }
  };

  draw(null);
  const img = new Image();
  img.onload = () => draw(img);
  img.src = scene.ground.tile_url;

  canvas.addEventListener('click', (ev) => {
    const rect = canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left, py = ev.clientY - rect.top;
    let best = null, bestD = HIT_RADIUS_CSS_PX;
    for (const m of markers) {
      const d = Math.hypot(m.x - px, m.y - py);
      if (d < bestD) { best = m; bestD = d; }
    }
    if (!best) { captionEl.textContent = 'Tap a marker to see what was detected.'; return; }
    const conf = best.confidence === null ? '' : ` — ${Math.round(best.confidence * 100)}% confidence` +
      (best.confidence < 0.6 ? ' (low confidence)' : '');
    captionEl.textContent = `${best.label}${conf}${best.crashed ? ' — you crashed here' : ''}`;
  });

  container.replaceChildren(canvas);
  captionEl.textContent = 'Tap a marker to see what was detected.';
  const crashMarker = markers.find((m) => m.crashed) || null;
  return { canvas, markers, crashMarker };
}
