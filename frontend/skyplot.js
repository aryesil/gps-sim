// frontend/skyplot.js
let _lastSkyEntries = {};   // canvasId -> entries, for click-to-select (positions recomputed on click)
const _SYS_COLOR = {G:'#06c', R:'#c30', E:'#093', C:'#c60', J:'#606', S:'#888'};

// cn0ByPrn (optional): {prn: metric_db} from the inspect step's acquire()
// results -- colors each dot by measured signal strength instead of the
// flat blue used when only geometry (no IQ yet) is available.
window.drawSkyplot = function (canvasId, entries, cn0ByPrn) {
  _lastSkyEntries[canvasId] = entries;
  const c = document.getElementById(canvasId), g = c.getContext('2d');
  const cx = c.width / 2, cy = c.height / 2, R = Math.min(cx, cy) - 10;
  g.clearRect(0, 0, c.width, c.height);
  g.strokeStyle = '#ccc';
  [1, 2 / 3, 1 / 3].forEach(k => { g.beginPath(); g.arc(cx, cy, R * k, 0, 7); g.stroke(); });
  entries.forEach(e => {
    const r = R * (1 - e.el_deg / 90), a = (e.az_deg - 90) * Math.PI / 180;
    const x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
    const db = cn0ByPrn && cn0ByPrn[e.prn];
    g.fillStyle = (db === undefined) ? (_SYS_COLOR[e.sys] || '#06c') : _cn0Color(db);
    g.beginPath(); g.arc(x, y, 4, 0, 7); g.fill();
    g.fillStyle = '#333';
    g.fillText(e.svid || ('G' + e.prn), x + 5, y);
  });
};

// Click the nearest dot -> fill the LNAV/correlation-curve PRN field, so a
// user can point at a satellite on the skyplot instead of typing its PRN.
// Called once per card (from channels.js's addChannel()) rather than once
// at module load, since each card gets its own skyplot canvas.
window.attachSkyplotClickHandler = function (canvasId, prnInputId) {
  document.getElementById(canvasId).addEventListener('click', (ev) => {
    const entries = _lastSkyEntries[canvasId];
    if (!entries || !entries.length) return;
    const c = ev.target, rect = c.getBoundingClientRect();
    const cx = c.width / 2, cy = c.height / 2, R = Math.min(cx, cy) - 10;
    const mx = (ev.clientX - rect.left) * (c.width / rect.width);
    const my = (ev.clientY - rect.top) * (c.height / rect.height);
    let best = null, bestD = Infinity;
    entries.forEach(e => {
      const r = R * (1 - e.el_deg / 90), a = (e.az_deg - 90) * Math.PI / 180;
      const x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
      const d = Math.hypot(mx - x, my - y);
      if (d < bestD) { bestD = d; best = e; }
    });
    if (best && bestD < 20) {
      const prnInput = document.getElementById(prnInputId);
      // prnInput is <input type="number">; assigning a non-numeric svid
      // ("G01") blanks it. Keep the numeric prn here -- the dot *label*
      // still shows svid || 'G'+prn above.
      if (prnInput) prnInput.value = best.prn;
    }
  });
};

// Clamped 0-30 dB metric_db range -> red (weak) to green (strong).
function _cn0Color(db) {
  const t = Math.max(0, Math.min(1, db / 30));
  const r = Math.round(220 * (1 - t)), gr = Math.round(160 * t + 40);
  return `rgb(${r},${gr},40)`;
}
