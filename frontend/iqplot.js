// frontend/iqplot.js
let _lastWaveform = {};   // canvasId -> {i, q} -- for hover readout
let _lastSpectrum = {};   // canvasId -> {freqs, db} -- for hover readout

window.drawWaveform = function (canvasId, i, q) {
  _lastWaveform[canvasId] = { i, q };
  const { g, W, H } = fitCanvas(canvasId);
  g.clearRect(0, 0, W, H);
  const n = i.length, mid = H / 2;
  const maxAbs = Math.max(1, ...i.map(Math.abs), ...q.map(Math.abs));
  const scale = (H / 2 - 4) / maxAbs;
  const plot = (arr, color) => {
    g.strokeStyle = color; g.lineWidth = 1; g.beginPath();
    arr.forEach((v, k) => {
      const x = (k / n) * W, y = mid - v * scale;
      k === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
    });
    g.stroke();
  };
  plot(i, '#06c'); plot(q, '#c60');
};

window.attachWaveformHover = function (canvasId, readoutId) {
  document.getElementById(canvasId).addEventListener('mousemove', (ev) => {
    const last = _lastWaveform[canvasId];
    if (!last) return;
    const c = ev.target, rect = c.getBoundingClientRect();
    const k = Math.round(((ev.clientX - rect.left) / rect.width) * (last.i.length - 1));
    if (k < 0 || k >= last.i.length) return;
    document.getElementById(readoutId).textContent =
      `sample ${k}: I=${last.i[k].toFixed(1)} Q=${last.q[k].toFixed(1)}`;
  });
};

window.drawConstellation = function (canvasId, i, q) {
  const { g, W, H } = fitCanvas(canvasId);
  g.clearRect(0, 0, W, H);
  const cx = W / 2, cy = H / 2;
  const maxAbs = Math.max(1, ...i.map(Math.abs), ...q.map(Math.abs));
  const scale = (Math.min(cx, cy) - 4) / maxAbs;
  g.fillStyle = 'rgba(0,80,200,0.35)';
  for (let k = 0; k < i.length; k++) {
    g.fillRect(cx + i[k] * scale, cy - q[k] * scale, 1.5, 1.5);
  }
};

// Accepts either the legacy form (canvasId, freqs, db) -- a single series
// with no RF offset -- or (canvasId, seriesArray) where each entry is
// {freqs, db, centre_hz, label, color}. Multiple series share ONE x-axis in
// absolute RF Hz = centre_hz + f_baseband, so carriers on different bands
// (L1 1575.42 MHz, GLONASS G1 1602 MHz) are both visible at once.
window.drawSpectrum = function (canvasId, freqs, db) {
  let series;
  if (Array.isArray(freqs) && freqs.length && typeof freqs[0] === 'object') {
    series = freqs.map(s => ({
      freqs: s.freqs, db: s.db,
      centre_hz: s.centre_hz || 0,
      label: s.label || '', color: s.color || '#06c',
    }));
  } else {
    series = [{ freqs, db, centre_hz: 0, label: '', color: '#06c' }];
  }
  const merged = series.map(s => ({
    abs: s.freqs.map(f => s.centre_hz + f),
    db: s.db, label: s.label, color: s.color,
  }));
  _lastSpectrum[canvasId] = { series: merged };

  const { g, W, H } = fitCanvas(canvasId);
  g.clearRect(0, 0, W, H);
  g.font = '10px system-ui, sans-serif';
  let xmin = Infinity, xmax = -Infinity, lo = Infinity, hi = -Infinity;
  merged.forEach(s => {
    s.abs.forEach(f => { if (f < xmin) xmin = f; if (f > xmax) xmax = f; });
    s.db.forEach(v => { if (v < lo) lo = v; if (v > hi) hi = v; });
  });
  const xspan = Math.max(1e-6, xmax - xmin);
  const span = Math.max(1e-6, hi - lo);
  merged.forEach(s => {
    g.strokeStyle = s.color; g.lineWidth = 1; g.beginPath();
    s.db.forEach((v, k) => {
      const x = ((s.abs[k] - xmin) / xspan) * W;
      const y = H - ((v - lo) / span) * H;
      k === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
    });
    g.stroke();
  });
  // legend (top-left): colour swatch + label per series
  merged.forEach((s, k) => {
    if (!s.label) return;
    const ly = 10 + k * 12;
    g.fillStyle = s.color; g.fillRect(4, ly - 6, 8, 8);
    g.fillStyle = '#333'; g.fillText(s.label, 16, ly + 1);
  });
};

window.attachSpectrumHover = function (canvasId, readoutId) {
  document.getElementById(canvasId).addEventListener('mousemove', (ev) => {
    const last = _lastSpectrum[canvasId];
    if (!last) return;
    const c = ev.target, rect = c.getBoundingClientRect();
    const frac = (ev.clientX - rect.left) / rect.width;
    const s = last.series[0];
    if (!s) return;
    const k = Math.round(frac * (s.abs.length - 1));
    if (k < 0 || k >= s.abs.length) return;
    document.getElementById(readoutId).textContent =
      `${(s.abs[k] / 1e6).toFixed(3)} MHz: ${s.db[k].toFixed(1)} dB`;
  });
};

window.drawCorrelationCurve = function (canvasId, chips, amp) {
  const { g, W, H } = fitCanvas(canvasId);
  g.clearRect(0, 0, W, H);
  if (!amp.length) return;
  const hi = Math.max(...amp);
  g.strokeStyle = '#a06'; g.lineWidth = 1; g.beginPath();
  amp.forEach((v, k) => {
    const x = (chips[k] / 1023) * W, y = H - (v / hi) * (H - 4);
    k === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
  });
  g.stroke();
};

window.loadCorrelationCurve = async function (canvasId, readoutId, outdir, prn) {
  const r = await fetch(`/api/correlation?outdir=${outdir}&prn=${prn}`);
  const out = document.getElementById(readoutId);
  if (!r.ok) { out.textContent = 'correlation error ' + r.status; return; }
  const d = await r.json();
  drawCorrelationCurve(canvasId, d.code_phase_chips, d.amplitude);
  out.textContent = `G${d.prn}: Doppler ${d.doppler_hz.toFixed(0)} Hz, peak ${d.metric_db.toFixed(1)} dB`;
};

// GPS-only runs: one bar per acquired PRN, height = metric_db from the
// inspect step (backend/inspector.py:compare -> acquire).
// Multi-GNSS runs: inspector is GPS L1 C/A only, so there is no per-PRN
// correlation. Fall back to the per-SV planned power (gain_db) from
// meta.json provenance.svs, one bar per satellite across every system,
// coloured by constellation.
const _BAR_SYS_COLOR = { G:'#2a6', R:'#c30', E:'#093', C:'#c60', J:'#606', S:'#888' };

// JS port of backend/synth/native/fading.cpp (splitmix64 + smoothstep between
// coherence knots). Keyed only on (seed, prn, knot), so evaluating it here at
// any run time t_s reproduces the exact per-block lognormal gain the C++ mixer
// folded into the IQ -- lets the per-SV power bars track the waveform scrubber.
const _M64 = (1n << 64n) - 1n;
function _mix64(x) {
  x = (x + 0x9E3779B97F4A7C15n) & _M64;
  x = ((x ^ (x >> 30n)) * 0xBF58476D1CE4E5B9n) & _M64;
  x = ((x ^ (x >> 27n)) * 0x94D049BB133111EBn) & _M64;
  return (x ^ (x >> 31n)) & _M64;
}
function _u01(h) { return Number(h >> 11n) * (1 / 9007199254740992); }
function _fadeGauss(seed, prn, knot) {
  const kn = (BigInt(knot) * 0x100000001B3n) & _M64;
  const base = _mix64((BigInt.asUintN(64, BigInt(seed))
    ^ (BigInt(prn) << 40n) ^ kn) & _M64);
  const u1 = _u01(_mix64(base)) + 1e-12;
  const u2 = _u01(_mix64(base ^ 0xABCDEFn));
  return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
}
window.fadingGainDb = function (sv, t_s) {
  const sigma = sv.fading_sigma_db, coh = sv.fading_coherence_s;
  if (!sv.fading_model || !(sigma > 0) || !(coh > 0)) return 0;
  const x = t_s / coh, k0 = Math.floor(x), frac = x - k0;
  const g0 = _fadeGauss(sv.fading_seed, sv.prn, k0);
  const g1 = _fadeGauss(sv.fading_seed, sv.prn, k0 + 1);
  const w = frac * frac * (3 - 2 * frac);
  return sigma * 1.1602387022306428 * (g0 * (1 - w) + g1 * w);
};

// channelId -> per-SV list (meta.json provenance.svs), for the scrubber to
// redraw the power bars at each sample position. null for GPS-only runs
// (those show the real acquisition metric instead).
const _svPowerModel = {};
window.setSvPowerModel = function (channelId, svs) {
  _svPowerModel[channelId] = (svs && svs.length) ? svs : null;
};

window.drawCorrelationBars = function (canvasId, rows, svs, t_s) {
  const { g, W, H } = fitCanvas(canvasId);
  g.clearRect(0, 0, W, H);
  g.font = '10px system-ui, sans-serif';
  let items;
  if (rows && rows.length) {
    items = rows.map(r => ({ label: 'G' + r.prn, val: r.metric_db, color: '#2a6' }));
  } else if (svs && svs.length) {
    items = svs.map(s => {
      let v = (typeof s.gain_db === 'number') ? s.gain_db
        : (typeof s.gain === 'number' ? 20 * Math.log10(s.gain) : 0);
      if (typeof t_s === 'number') v += fadingGainDb(s, t_s);
      return { label: s.sys + s.prn, val: v, color: _BAR_SYS_COLOR[s.sys] || '#2a6' };
    });
  } else { return; }
  items.sort((a, b) => (a.label < b.label ? -1 : 1));
  // Zoom the y-axis to the actual spread -- for GPS metric_db the peak/floor
  // ratio saturates near the same value, and for gain_db the elevation
  // taper spread is only a few dB; a min-anchored window (>= 6 dB) keeps the
  // per-SV differences legible.
  const w = W / items.length;
  const vals = items.map(x => x.val);
  const hi = Math.max(...vals) + 1;
  const lo = Math.min(Math.min(...vals) - 2, hi - 6);
  const span = Math.max(1e-6, hi - lo);
  items.forEach((x, k) => {
    const h = Math.max(0, (x.val - lo) / span) * (H - 22);
    g.fillStyle = x.color;
    g.fillRect(k * w + 2, H - h - 10, Math.max(1, w - 4), h);
    g.fillStyle = '#333';
    g.fillText(x.val.toFixed(1), k * w + 2, H - h - 13);
    g.fillStyle = '#666';
    g.fillText(x.label, k * w + 2, H - 2);
  });
};

// Live spectrogram (waterfall): each call scrolls the canvas one column
// left and paints a new heat-mapped column at the right edge from the
// per-segment FFT the backend streams over /api/live/start's SSE.
function _heatColor(v) {
  v = Math.max(0, Math.min(1, v));
  const r = Math.round(255 * Math.min(1, v * 2));
  const b = Math.round(255 * Math.min(1, (1 - v) * 2));
  const g = Math.round(255 * (1 - Math.abs(v - 0.5) * 2));
  return `rgb(${r},${g},${b})`;
}

window.pushSpectrogramColumn = function (canvasId, db) {
  const c = document.getElementById(canvasId);
  if (!c) return;
  const g = c.getContext('2d');
  g.drawImage(c, 1, 0, c.width - 1, c.height, 0, 0, c.width - 1, c.height);
  const lo = Math.min(...db), hi = Math.max(...db);
  const span = Math.max(1e-6, hi - lo);
  const h = c.height, n = db.length;
  for (let y = 0; y < h; y++) {
    const idx = Math.min(n - 1, Math.floor((1 - y / h) * (n - 1)));
    g.fillStyle = _heatColor((db[idx] - lo) / span);
    g.fillRect(c.width - 1, y, 1, 1);
  }
};

// C/N0 trend: scrolling line chart, one sample per live segment (~1/s).
// Keeps its own history per canvas so a redraw can rescale to the visible
// window's min/max instead of a fixed dB range.
const _cn0History = {};   // canvasId -> number[]

window.pushCn0Sample = function (canvasId, db) {
  const c = document.getElementById(canvasId);
  if (!c) return;
  const hist = _cn0History[canvasId] || (_cn0History[canvasId] = []);
  hist.push(db);
  while (hist.length > c.width) hist.shift();
  const g = c.getContext('2d');
  g.clearRect(0, 0, c.width, c.height);
  const lo = Math.min(...hist), hi = Math.max(...hist);
  const span = Math.max(1e-6, hi - lo);
  g.strokeStyle = '#8b7cf6'; g.beginPath();
  hist.forEach((v, k) => {
    const x = (k / Math.max(1, hist.length - 1)) * c.width;
    const y = c.height - ((v - lo) / span) * (c.height - 8) - 4;
    k === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
  });
  g.stroke();
  const readout = document.getElementById(canvasId.replace('-trend', '-readout'));
  if (readout) readout.textContent = ` ${db.toFixed(1)} dB`;
};

// Scrubber: after Generate, the whole .bin file exists on disk -- let the
// user drag a slider across it and re-fetch a 2000-sample window at that
// offset instead of always looking at sample 0.
window.loadIqPlots = async function (channelId, outdir, offset) {
  const r = await fetch(`/api/iqplot?outdir=${outdir}&offset=${offset || 0}`);
  if (!r.ok) return;
  const d = await r.json();
  drawWaveform(`${channelId}-iq-waveform`, d.i, d.q);
  drawConstellation(`${channelId}-iq-constellation`, d.i, d.q);
  if (Array.isArray(d.bands) && d.bands.length >= 1) {
    // "all bands together": overlay every RF band's spectrum on one shared
    // absolute-frequency axis. Waveform/constellation stay on the primary
    // band only (top-level d.i/d.q) -- overlaying different-fs time series
    // is not meaningful.
    drawSpectrum(`${channelId}-iq-spectrum`, d.bands.map((b, k) => ({
      freqs: b.spectrum_freq_hz, db: b.spectrum_db, centre_hz: b.centre_hz,
      label: b.id + ' (' + (b.systems || []).join('') + ')',
      color: ['#06c', '#c60', '#093', '#a06'][k % 4],
    })));
    const wf = document.getElementById(`${channelId}-iq-waveform-readout`);
    const primary = d.bands.find(b => b.id === 'L1') || d.bands[0];
    // Set, do not prepend: this runs on every scrubber `oninput`, and
    // prepending stacked "L1: L1: L1: ..." without bound. The hover handler
    // overwrites this line with the per-sample readout anyway.
    if (wf && primary) wf.textContent = 'primary band: ' + primary.id;
  } else {
    drawSpectrum(`${channelId}-iq-spectrum`, d.spectrum_freq_hz, d.spectrum_db);
  }

  const slider = document.getElementById(`${channelId}-iq-scrub`);
  const readout = document.getElementById(`${channelId}-iq-scrub-readout`);
  if (slider) {
    slider.max = Math.max(0, d.total_samples - 2000);
    slider.value = d.offset;
  }
  if (readout) {
    // Window RMS in dBFS. GPS L1 C/A baseband is a wideband spread-spectrum
    // sum with a near-constant envelope, so this figure barely moves as the
    // scrubber is dragged -- that is expected, not a stuck plot.
    let ss = 0;
    for (let k = 0; k < d.i.length; k++) ss += d.i[k] * d.i[k] + d.q[k] * d.q[k];
    const rms = Math.sqrt(ss / Math.max(1, d.i.length));
    const fs = d.sample_format === 'int8' ? 127 : 32767;
    readout.textContent =
      `t=${(d.offset / d.sample_rate).toFixed(2)}s / ${(d.total_samples / d.sample_rate).toFixed(2)}s` +
      `   RMS ${(20 * Math.log10(rms / fs)).toFixed(1)} dBFS`;
  }

  // Per-SV signal power at this scrub position: re-evaluate the fading model
  // at t = offset / sample_rate so the bars breathe with the waveform as the
  // scrubber is dragged, instead of showing a single frozen snapshot.
  const model = _svPowerModel[channelId];
  if (model && d.sample_rate) {
    const t = d.offset / d.sample_rate;
    drawCorrelationBars(`${channelId}-iq-correlation`, null, model, t);
    const lbl = document.getElementById(`${channelId}-iq-correlation-label`);
    if (lbl) lbl.textContent =
      `Per-SV signal power @ t=${t.toFixed(2)}s (gain + fading, dB)`;
  }
};

window.attachIqScrubber = function (channelId, outdir) {
  const slider = document.getElementById(`${channelId}-iq-scrub`);
  if (!slider) return;
  slider.oninput = () => loadIqPlots(channelId, outdir, Number(slider.value));
};
