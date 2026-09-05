// Interactive visualisation of /api/precise/compare (broadcast-realigned
// vs precise SP3 reference). Pure canvas 2d, same house style as
// iqplot.js / skyplot.js -- no external chart library.
//
// window.renderCompare(containerId, data) wipes the container and fills it
// with: summary cards, a per-PRN radial/along/cross bar chart, a per-PRN
// clock-offset bar chart, and -- when data.series is present -- a
// time-sweep line chart with per-PRN toggles, a metric selector and a
// hover readout.

(function () {
  const PALETTE = ['#6cf', '#fc6', '#9f9', '#f99', '#c9f', '#9cf', '#fd8',
                   '#8fd', '#f8b', '#bdf', '#df8', '#dd6'];
  const prnColor = (prn, i) => PALETTE[(i != null ? i : prn) % PALETTE.length];

  // Size a canvas to its container at the display's pixel density, then
  // scale the 2d context so all drawing code works in CSS pixels. Draw
  // functions read canvas._logW / canvas._logH for the logical size.
  function setupCanvas(canvas, logicalH) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cssW = Math.max(600, Math.floor(
      (canvas.parentElement && canvas.parentElement.clientWidth) || 900));
    canvas.style.width = '100%';
    canvas.style.height = logicalH + 'px';
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(logicalH * dpr);
    canvas.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
    canvas._logW = cssW;
    canvas._logH = logicalH;
    return canvas;
  }

  function el(tag, cls, txt) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }

  function card(container, label, value) {
    const c = el('div', 'compare-card');
    c.appendChild(el('b', null, value));
    c.appendChild(document.createTextNode(label));
    container.appendChild(c);
  }

  // --- generic bar chart: groups of bars, one group per PRN -----------
  function drawGroupedBars(canvas, prns, groups, opts) {
    opts = opts || {};
    const g = canvas.getContext('2d');
    const W = canvas._logW || canvas.width, H = canvas._logH || canvas.height;
    g.clearRect(0, 0, W, H);
    const padL = 54, padR = 12, padT = 18, padB = 28;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    let vmax = opts.symmetric ? 1e-9 : 0, vmin = 0;
    groups.forEach(s => s.values.forEach(v => {
      if (v > vmax) vmax = v;
      if (v < vmin) vmin = v;
    }));
    if (opts.symmetric) { const m = Math.max(vmax, -vmin); vmax = m; vmin = -m; }
    if (vmax === vmin) { vmax += 1; vmin -= 1; }
    const y = v => padT + plotH * (1 - (v - vmin) / (vmax - vmin));
    // zero line + labels
    g.strokeStyle = '#555'; g.fillStyle = '#999';
    g.font = '11px "IBM Plex Mono", monospace';
    g.beginPath(); g.moveTo(padL, y(0)); g.lineTo(W - padR, y(0)); g.stroke();
    for (let k = 0; k <= 4; k++) {
      const v = vmin + (vmax - vmin) * k / 4;
      const yy = y(v);
      g.strokeStyle = '#2c2c2c';
      g.beginPath(); g.moveTo(padL, yy); g.lineTo(W - padR, yy); g.stroke();
      g.fillStyle = '#999';
      g.fillText(v.toFixed(Math.abs(vmax) < 10 ? 2 : 0), 6, yy + 4);
    }
    const nG = prns.length || 1;
    const slot = plotW / nG;
    const nB = groups.length;
    const bw = Math.max(4, Math.min(28, slot / (nB + 1.2)));
    prns.forEach((prn, gi) => {
      const x0 = padL + slot * gi + (slot - bw * nB) / 2;
      groups.forEach((s, bi) => {
        const v = s.values[gi];
        g.fillStyle = s.color;
        const yy = y(v), y0 = y(0);
        g.fillRect(x0 + bi * bw, Math.min(yy, y0), bw, Math.abs(yy - y0) || 1);
      });
      g.fillStyle = '#aaa';
      g.fillText(String(prn), x0 + (bw * nB) / 2 - g.measureText(String(prn)).width / 2, H - 10);
    });
    // legend
    let lx = padL;
    groups.forEach(s => {
      g.fillStyle = s.color; g.fillRect(lx, 3, 10, 10);
      g.fillStyle = '#ccc'; g.fillText(s.label, lx + 14, 12);
      lx += 20 + g.measureText(s.label).width + 14;
    });
    if (opts.unit) { g.fillStyle = '#777'; g.fillText(opts.unit, W - padR - 26, 12); }
  }

  // --- time-sweep line chart ----------------------------------------
  const METRICS = [
    ['pos_delta_m', '|Δpos| m', false],
    ['pos_delta_radial_m', 'Δradial m', true],
    ['pos_delta_along_m', 'Δalong m', true],
    ['pos_delta_cross_m', 'Δcross m', true],
    ['range_delta_m', 'Δrange m', true],
    ['clock_delta_ns', 'Δclock ns', true],
    ['doppler_delta_hz', 'Δdoppler Hz', true],
  ];

  function drawSeries(canvas, series, prnsOn, metric, symmetric, readoutEl) {
    const g = canvas.getContext('2d');
    const W = canvas._logW || canvas.width, H = canvas._logH || canvas.height;
    g.clearRect(0, 0, W, H);
    const padL = 56, padR = 14, padT = 14, padB = 26;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const prns = Object.keys(series).filter(p => prnsOn[p]);
    let tmax = 0, vmax = -Infinity, vmin = Infinity;
    prns.forEach(p => series[p].forEach(pt => {
      tmax = Math.max(tmax, pt.t_offset_s);
      const v = pt[metric];
      if (v > vmax) vmax = v;
      if (v < vmin) vmin = v;
    }));
    if (!isFinite(vmax)) { g.fillStyle = '#888'; g.fillText('no PRN selected', padL, H / 2); return; }
    if (symmetric) { const m = Math.max(Math.abs(vmax), Math.abs(vmin), 1e-9); vmax = m; vmin = -m; }
    else { vmin = Math.min(vmin, 0); if (vmax === vmin) vmax += 1; }
    tmax = tmax || 1;
    const x = t => padL + plotW * t / tmax;
    const y = v => padT + plotH * (1 - (v - vmin) / (vmax - vmin));
    g.strokeStyle = '#2c2c2c'; g.fillStyle = '#999';
    g.font = '11px "IBM Plex Mono", monospace';
    for (let k = 0; k <= 4; k++) {
      const v = vmin + (vmax - vmin) * k / 4, yy = y(v);
      g.beginPath(); g.moveTo(padL, yy); g.lineTo(W - padR, yy); g.stroke();
      g.fillText(v.toFixed(Math.abs(vmax) < 10 ? 2 : 0), 6, yy + 4);
    }
    g.strokeStyle = '#555';
    g.beginPath(); g.moveTo(padL, y(0)); g.lineTo(W - padR, y(0)); g.stroke();
    for (let k = 0; k <= 4; k++) {
      const t = tmax * k / 4;
      g.fillText(t.toFixed(0) + 's', x(t) - 8, H - 6);
    }
    prns.forEach((p, i) => {
      g.strokeStyle = prnColor(+p, i); g.lineWidth = 1.5;
      g.beginPath();
      series[p].forEach((pt, j) => {
        const xx = x(pt.t_offset_s), yy = y(pt[metric]);
        j ? g.lineTo(xx, yy) : g.moveTo(xx, yy);
      });
      g.stroke();
      const last = series[p][series[p].length - 1];
      g.fillStyle = prnColor(+p, i);
      g.fillText('PRN ' + p, x(last.t_offset_s) - 40, y(last[metric]) - 3);
    });
    canvas._cmp = { series, prns, metric, x, y, tmax, padL, padR, W, readoutEl };
  }

  function attachSeriesHover(canvas) {
    canvas.onmousemove = (ev) => {
      const s = canvas._cmp;
      if (!s || !s.readoutEl) return;
      const rect = canvas.getBoundingClientRect();
      const px = (ev.clientX - rect.left) * s.W / rect.width;
      const frac = Math.max(0, Math.min(1, (px - s.padL) / (s.W - s.padL - s.padR)));
      const t = frac * s.tmax;
      const parts = s.prns.map(p => {
        let best = s.series[p][0];
        s.series[p].forEach(pt => {
          if (Math.abs(pt.t_offset_s - t) < Math.abs(best.t_offset_s - t)) best = pt;
        });
        return `PRN ${p} ${best[s.metric].toFixed(2)}`;
      });
      s.readoutEl.textContent = `t≈${t.toFixed(0)}s   ` + parts.join('   ');
    };
  }

  // C/A code chip length in metres (c / 1.023 MHz) -- the receiver's
  // ranging quantum, a concrete yardstick for "how big is this error".
  const CHIP_M = 293.0522;

  // Plain-language verdict for a range-error RMS, in metres.
  function verdict(rangeRms) {
    if (rangeRms < 1) return ['excellent',
      'Broadcast ephemeris is already almost identical to the precise reference here — switching this channel to precise mode barely changes the simulated ranges.'];
    if (rangeRms < 5) return ['good',
      'Broadcast ephemeris is close to precise. Precise mode tightens each satellite range by a few metres.'];
    if (rangeRms < 20) return ['marginal',
      'Broadcast ephemeris is drifting from precise. Precise mode is worth using for this scenario.'];
    return ['poor',
      'Broadcast ephemeris is well off the precise reference at this epoch. Use precise mode.'];
  }

  function panel(grid, label, wide) {
    const p = el('div', 'compare-panel' + (wide ? ' wide' : ''));
    if (label) p.appendChild(el('div', 'compare-panel-label', label));
    grid.appendChild(p);
    return p;
  }

  window.renderCompare = function (containerId, d) {
    const root = document.getElementById(containerId);
    root.innerHTML = '';
    root.className = 'compare-out';

    const s = d.summary || {};

    // --- headline: what the numbers mean, in plain language ----------
    if (s.n) {
      const rangeRms = s.range_delta_rms_m;
      const pctChip = rangeRms / CHIP_M * 100;
      const [grade, sentence] = verdict(rangeRms);

      const verdictEl = el('div', 'compare-verdict compare-verdict-' + grade);
      verdictEl.appendChild(el('b', null,
        `Broadcast ranges sit ~${rangeRms.toFixed(1)} m off precise` +
        `  (≈ ${pctChip.toFixed(1)}% of one GPS code chip)`));
      verdictEl.appendChild(el('div', null, sentence));
      root.appendChild(verdictEl);
    }

    const cards = el('div', 'compare-cards');
    root.appendChild(cards);
    if (s.n) {
      card(cards, 'satellites compared', s.n);
      card(cards, 'range error (typical)', s.range_delta_rms_m.toFixed(1) + ' m');
      card(cards, 'orbit position error', s.pos_delta_rms_m.toFixed(1) + ' m');
      card(cards, 'speed (Doppler) error', s.doppler_delta_rms_hz.toFixed(2) + ' Hz');
    } else {
      cards.appendChild(el('div', 'compare-card', 'no overlapping visible PRNs'));
    }
    card(cards, 'broadcast', d.broadcast_source || '?');
    card(cards, 'precise ref', d.precise_source || '?');

    (d.warnings || []).forEach(w => root.appendChild(el('div', 'hint', '⚠ ' + w)));
    if (d.note) root.appendChild(el('div', 'hint', d.note));

    // --- charts laid out side by side in a responsive grid ----------
    const grid = el('div', 'compare-grid');
    root.appendChild(grid);

    const rows = (d.rows || []).slice().sort((a, b) => a.prn - b.prn);
    if (rows.length) {
      const prns = rows.map(r => r.prn);
      const racP = panel(grid, 'Per-PRN position error, broadcast − precise (radial / along / cross)', true);
      const racC = el('canvas'); racP.appendChild(racC); setupCanvas(racC, 280);
      drawGroupedBars(racC, prns, [
        { label: 'radial', color: '#6cf', values: rows.map(r => r.pos_delta_radial_m) },
        { label: 'along', color: '#fc6', values: rows.map(r => r.pos_delta_along_m) },
        { label: 'cross', color: '#9f9', values: rows.map(r => r.pos_delta_cross_m) },
      ], { symmetric: true, unit: 'm' });

      const clkP = panel(grid, 'Per-PRN clock-offset difference', true);
      const clkC = el('canvas'); clkP.appendChild(clkC); setupCanvas(clkC, 220);
      drawGroupedBars(clkC, prns, [
        { label: 'Δclock', color: '#c9f', values: rows.map(r => r.clock_delta_s * 1e9) },
      ], { symmetric: true, unit: 'ns' });
    }

    // --- optional time sweep -------------------------------------------
    const series = {};
    Object.keys(d.series || {}).forEach(p => {
      series[p] = d.series[p].map(pt => ({ ...pt, clock_delta_ns: pt.clock_delta_s * 1e9 }));
    });
    if (Object.keys(series).length) {
      const swP = panel(grid,
        `Time sweep — same comparison every ${d.step_s}s out to ${d.sweep_s}s`, true);
      const ctrls = el('div', 'compare-controls');
      swP.appendChild(ctrls);
      const tsC = el('canvas'); swP.appendChild(tsC); setupCanvas(tsC, 280);
      const readout = el('div', 'compare-readout');
      swP.appendChild(readout);

      const prnsOn = {};
      Object.keys(series).forEach(p => { prnsOn[p] = true; });
      let metricIdx = 0;

      const redraw = () => {
        const [key, , sym] = METRICS[metricIdx];
        drawSeries(tsC, series, prnsOn, key, sym, readout);
        attachSeriesHover(tsC);
      };

      const mSel = el('select');
      METRICS.forEach((m, i) => {
        const o = el('option', null, m[1]); o.value = i; mSel.appendChild(o);
      });
      mSel.onchange = () => { metricIdx = +mSel.value; redraw(); };
      ctrls.appendChild(mSel);

      Object.keys(series).sort((a, b) => a - b).forEach((p, i) => {
        const b = el('button', 'on', 'PRN ' + p);
        b.style.color = prnColor(+p, i);
        b.onclick = () => {
          prnsOn[p] = !prnsOn[p];
          b.classList.toggle('on', prnsOn[p]);
          redraw();
        };
        ctrls.appendChild(b);
      });
      redraw();
    }
  };
})();
