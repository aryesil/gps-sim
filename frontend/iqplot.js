// frontend/iqplot.js
window.drawWaveform = function (i, q) {
  const c = document.getElementById('iq-waveform'), g = c.getContext('2d');
  g.clearRect(0, 0, c.width, c.height);
  const n = i.length, mid = c.height / 2;
  const maxAbs = Math.max(1, ...i.map(Math.abs), ...q.map(Math.abs));
  const scale = (c.height / 2 - 4) / maxAbs;
  const plot = (arr, color) => {
    g.strokeStyle = color; g.beginPath();
    arr.forEach((v, k) => {
      const x = (k / n) * c.width, y = mid - v * scale;
      k === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
    });
    g.stroke();
  };
  plot(i, '#06c'); plot(q, '#c60');
};

window.drawConstellation = function (i, q) {
  const c = document.getElementById('iq-constellation'), g = c.getContext('2d');
  g.clearRect(0, 0, c.width, c.height);
  const cx = c.width / 2, cy = c.height / 2;
  const maxAbs = Math.max(1, ...i.map(Math.abs), ...q.map(Math.abs));
  const scale = (Math.min(cx, cy) - 4) / maxAbs;
  g.fillStyle = 'rgba(0,80,200,0.35)';
  for (let k = 0; k < i.length; k++) {
    g.fillRect(cx + i[k] * scale, cy - q[k] * scale, 1.5, 1.5);
  }
};

window.drawSpectrum = function (freqs, db) {
  const c = document.getElementById('iq-spectrum'), g = c.getContext('2d');
  g.clearRect(0, 0, c.width, c.height);
  const lo = Math.min(...db), hi = Math.max(...db);
  const span = Math.max(1e-6, hi - lo);
  g.strokeStyle = '#06c'; g.beginPath();
  db.forEach((v, k) => {
    const x = (k / db.length) * c.width;
    const y = c.height - ((v - lo) / span) * c.height;
    k === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
  });
  g.stroke();
};

// Reuses metric_db already computed per-PRN by /api/generate's inspect step
// (backend/inspector.py:compare -> acquire) -- no extra backend call needed.
window.drawCorrelationBars = function (rows) {
  const c = document.getElementById('iq-correlation'), g = c.getContext('2d');
  g.clearRect(0, 0, c.width, c.height);
  if (!rows || !rows.length) return;
  const w = c.width / rows.length;
  const maxDb = Math.max(1, ...rows.map(r => r.metric_db));
  rows.forEach((r, k) => {
    const h = (Math.max(0, r.metric_db) / maxDb) * (c.height - 14);
    g.fillStyle = '#2a6';
    g.fillRect(k * w + 2, c.height - h, w - 4, h);
    g.fillStyle = '#333';
    g.fillText('G' + r.prn, k * w + 2, c.height - 2);
  });
};

window.loadIqPlots = async function (outdir) {
  const r = await fetch(`/api/iqplot?outdir=${outdir}`);
  if (!r.ok) return;
  const d = await r.json();
  drawWaveform(d.i, d.q);
  drawConstellation(d.i, d.q);
  drawSpectrum(d.spectrum_freq_hz, d.spectrum_db);
};
