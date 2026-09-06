// frontend/plots.js
window.drawInspectTable = function (tableId, rows) {
  const t = document.getElementById(tableId);
  if (!rows || !rows.length) {
    // No per-PRN L1 C/A correlation table: a precise multi-GNSS run skips
    // that GPS-only check (it is not meaningful for a mixed stream). The
    // waveform / spectrum plots below still load from gpssim.bin.
    t.innerHTML = '<tr><td class="muted">no L1 C/A correlation table for '
      + 'this run — see the waveform and spectrum plots below</td></tr>';
    return;
  }
  t.innerHTML = '<tr><th>PRN</th><th>exp chip</th><th>meas chip</th><th>Δchip</th>'
    + '<th>exp Hz</th><th>meas Hz</th><th>ΔHz</th><th>dB</th></tr>'
    + rows.map(r => `<tr><td>G${r.prn}</td><td>${r.expected_code_phase_chips.toFixed(2)}</td>`
      + `<td>${r.measured_code_phase_chips.toFixed(2)}</td><td>${r.code_phase_err_chips.toFixed(2)}</td>`
      + `<td>${r.expected_doppler_hz.toFixed(0)}</td><td>${r.measured_doppler_hz.toFixed(0)}</td>`
      + `<td>${r.doppler_err_hz.toFixed(0)}</td><td>${r.metric_db.toFixed(1)}</td></tr>`).join('');
};

// Per-SV signal-power table. inspector.compare is GPS L1 C/A only, so for a
// multi-constellation run the status panel would otherwise show nothing but
// GPS. This renders every generated satellite (all systems) straight from
// meta.json, with the static elevation taper gain and the configured fading
// sigma so the per-SV power spread is visible.
window.drawSvPowerTable = function (tableId, svs, bands) {
  const t = document.getElementById(tableId);
  if (!t) return;
  if (!svs || !svs.length) { t.innerHTML = ''; return; }
  const SYS = { G: 'GPS', R: 'GLONASS', E: 'Galileo', C: 'BeiDou',
                J: 'QZSS', S: 'SBAS', I: 'NavIC' };
  const bandFor = {};
  (bands || []).forEach(b => (b.systems || []).forEach(s => { bandFor[s] = b.id; }));
  const rows = svs.slice().sort((a, b) =>
    (a.sys + '').localeCompare(b.sys + '') || a.prn - b.prn);
  t.innerHTML =
    '<tr><th>SV</th><th>sys</th><th>band</th><th>el °</th><th>az °</th>'
    + '<th>gain dB</th><th>fade σ dB</th><th>code Doppler Hz</th></tr>'
    + rows.map(s => {
        const gdb = (typeof s.gain_db === 'number') ? s.gain_db.toFixed(2)
          : (typeof s.gain === 'number' ? (20 * Math.log10(s.gain)).toFixed(2) : '—');
        const sig = (typeof s.fading_sigma_db === 'number' && s.fading_sigma_db > 0)
          ? s.fading_sigma_db.toFixed(1) : '0';
        const el = (typeof s.el_deg === 'number') ? s.el_deg.toFixed(1) : '—';
        const az = (typeof s.az_deg === 'number') ? s.az_deg.toFixed(1) : '—';
        const cd = (typeof s.code_doppler_hz === 'number') ? s.code_doppler_hz.toFixed(1) : '—';
        return `<tr><td>${s.sys}${s.prn}</td><td>${SYS[s.sys] || s.sys}</td>`
          + `<td>${bandFor[s.sys] || 'L1'}</td><td>${el}</td><td>${az}</td>`
          + `<td>${gdb}</td><td>${sig}</td><td>${cd}</td></tr>`;
      }).join('');
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
