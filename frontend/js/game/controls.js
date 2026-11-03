// Input: arrow keys (desktop) + swipes (touch). Emits 'left' | 'right' | 'jump' | 'slide' | 'tap'.
const KEYS = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'jump', ArrowDown: 'slide', ' ': 'jump' };
const SWIPE_MIN_PX = 24;

// `isRunning()` gates preventDefault so arrow keys / space keep scrolling and activating buttons
// on the loading / ready / results overlays; commands are still forwarded (main.js filters by phase).
export function attachControls(surface, onCommand, isRunning = () => true) {
  let touchStart = null;

  const onKey = (ev) => {
    const cmd = KEYS[ev.key];
    if (!cmd) return;
    if (isRunning()) ev.preventDefault();
    onCommand(cmd);
  };
  const onTouchStart = (ev) => {
    const t = ev.changedTouches[0];
    touchStart = { x: t.clientX, y: t.clientY };
  };
  const onTouchEnd = (ev) => {
    if (!touchStart) return;
    const t = ev.changedTouches[0];
    const dx = t.clientX - touchStart.x;
    const dy = t.clientY - touchStart.y;
    touchStart = null;
    if (Math.max(Math.abs(dx), Math.abs(dy)) < SWIPE_MIN_PX) { onCommand('tap'); return; }
    if (Math.abs(dx) > Math.abs(dy)) onCommand(dx > 0 ? 'right' : 'left');
    else onCommand(dy < 0 ? 'jump' : 'slide');
  };
  const onPointerDown = (ev) => { if (ev.pointerType === 'mouse') onCommand('tap'); };

  window.addEventListener('keydown', onKey);
  surface.addEventListener('touchstart', onTouchStart, { passive: true });
  surface.addEventListener('touchend', onTouchEnd, { passive: true });
  surface.addEventListener('pointerdown', onPointerDown);
  return () => {
    window.removeEventListener('keydown', onKey);
    surface.removeEventListener('touchstart', onTouchStart);
    surface.removeEventListener('touchend', onTouchEnd);
    surface.removeEventListener('pointerdown', onPointerDown);
  };
}
