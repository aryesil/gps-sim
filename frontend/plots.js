// frontend/plots.js
window.drawInspectTable = function (rows) {
  const t = document.getElementById('inspect-table');
  t.innerHTML = '<tr><th>PRN</th><th>exp chip</th><th>meas chip</th><th>Δchip</th>'
    + '<th>exp Hz</th><th>meas Hz</th><th>ΔHz</th><th>dB</th></tr>'
    + rows.map(r => `<tr><td>G${r.prn}</td><td>${r.expected_code_phase_chips.toFixed(2)}</td>`
      + `<td>${r.measured_code_phase_chips.toFixed(2)}</td><td>${r.code_phase_err_chips.toFixed(2)}</td>`
      + `<td>${r.expected_doppler_hz.toFixed(0)}</td><td>${r.measured_doppler_hz.toFixed(0)}</td>`
      + `<td>${r.doppler_err_hz.toFixed(0)}</td><td>${r.metric_db.toFixed(1)}</td></tr>`).join('');
};
