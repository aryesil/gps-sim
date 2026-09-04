// frontend/skyplot.js
// cn0ByPrn (optional): {prn: metric_db} from the inspect step's acquire()
// results -- colors each dot by measured signal strength instead of the
// flat blue used when only geometry (no IQ yet) is available.
window.drawSkyplot = function (entries, cn0ByPrn) {
  const c = document.getElementById('skyplot'), g = c.getContext('2d');
  const cx = c.width / 2, cy = c.height / 2, R = Math.min(cx, cy) - 10;
  g.clearRect(0, 0, c.width, c.height);
  g.strokeStyle = '#ccc';
  [1, 2 / 3, 1 / 3].forEach(k => { g.beginPath(); g.arc(cx, cy, R * k, 0, 7); g.stroke(); });
  entries.forEach(e => {
    const r = R * (1 - e.el_deg / 90), a = (e.az_deg - 90) * Math.PI / 180;
    const x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
    const db = cn0ByPrn && cn0ByPrn[e.prn];
    g.fillStyle = (db === undefined) ? '#06c' : _cn0Color(db);
    g.beginPath(); g.arc(x, y, 4, 0, 7); g.fill();
    g.fillStyle = '#333';
    g.fillText('G' + e.prn, x + 5, y);
  });
};

// Clamped 0-30 dB metric_db range -> red (weak) to green (strong).
function _cn0Color(db) {
  const t = Math.max(0, Math.min(1, db / 30));
  const r = Math.round(220 * (1 - t)), gr = Math.round(160 * t + 40);
  return `rgb(${r},${gr},40)`;
}
