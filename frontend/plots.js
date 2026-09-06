// frontend/plots.js
window.drawInspectTable = function (tableId, rows) {
  const t = document.getElementById(tableId);
  t.innerHTML = '<tr><th>PRN</th><th>exp chip</th><th>meas chip</th><th>Δchip</th>'
    + '<th>exp Hz</th><th>meas Hz</th><th>ΔHz</th><th>dB</th></tr>'
    + rows.map(r => `<tr><td>G${r.prn}</td><td>${r.expected_code_phase_chips.toFixed(2)}</td>`
      + `<td>${r.measured_code_phase_chips.toFixed(2)}</td><td>${r.code_phase_err_chips.toFixed(2)}</td>`
      + `<td>${r.expected_doppler_hz.toFixed(0)}</td><td>${r.measured_doppler_hz.toFixed(0)}</td>`
      + `<td>${r.doppler_err_hz.toFixed(0)}</td><td>${r.metric_db.toFixed(1)}</td></tr>`).join('');
};

// Satellites tab renderers. /api/preview returns {satellites, dop, warnings};
// both of those were computed and shipped to the browser but never displayed
// after the per-card redesign.
window.drawDop = function (elId, dop) {
  const el = document.getElementById(elId);
  if (!dop) { el.textContent = ''; return; }
  // dop values arrive as null when non-finite (fewer than 4 satellites).
  const fmt = v => (typeof v === 'number' ? v.toFixed(2) : '—');
  el.textContent = `GDOP ${fmt(dop.gdop)} · PDOP ${fmt(dop.pdop)} · `
    + `HDOP ${fmt(dop.hdop)} · VDOP ${fmt(dop.vdop)} · TDOP ${fmt(dop.tdop)}`;
};

window.drawSatTable = function (tableId, sats) {
  const t = document.getElementById(tableId);
  t.innerHTML = '<tr><th>PRN</th><th>sys</th><th>az °</th><th>el °</th><th>range km</th>'
    + '<th>Doppler Hz</th><th>code chip</th></tr>'
    + (sats || []).map(s => `<tr><td>${s.svid || ('G' + s.prn)}</td><td>${s.sys || 'G'}</td><td>${s.az_deg.toFixed(1)}</td>`
      + `<td>${s.el_deg.toFixed(1)}</td><td>${(s.geo_range_m / 1000).toFixed(1)}</td>`
      + `<td>${s.carrier_doppler_hz.toFixed(0)}</td>`
      + `<td>${s.code_phase_chips.toFixed(1)}</td></tr>`).join('');
};
