// frontend/app.js
let lastOutdir = null;
window.addEventListener('DOMContentLoaded', () => {
  gpsMap.init();

  document.getElementById('btn-preview').onclick = async () => {
    const ll = gpsMap.latlng(); if (!ll) return alert('pick a point');
    const r = await fetch('/api/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lat: ll.lat, lon: ll.lng, alt: 100,
        start_utc: document.getElementById('start-utc').value + ':00',
      }),
    });
    const d = await r.json();
    drawSkyplot(d.satellites);
    const f2 = (x) => (typeof x === 'number' ? x.toFixed(2) : '—');
    document.getElementById('dop').textContent =
      `PDOP ${f2(d.dop && d.dop.pdop)}  GDOP ${f2(d.dop && d.dop.gdop)}`;
    document.getElementById('warnings').textContent = d.warnings.join(' · ');
    document.getElementById('sat-table').innerHTML =
      '<tr><th>PRN</th><th>az</th><th>el</th><th>PR km</th><th>chip</th><th>Doppler</th></tr>' +
      d.satellites.map(s => `<tr><td>G${s.prn}</td><td>${s.az_deg.toFixed(0)}</td>` +
        `<td>${s.el_deg.toFixed(0)}</td><td>${(s.pseudorange_m / 1e3).toFixed(1)}</td>` +
        `<td>${s.code_phase_chips.toFixed(1)}</td><td>${s.carrier_doppler_hz.toFixed(0)}</td></tr>`).join('');
  };

  document.getElementById('btn-generate').onclick = () => {
    const ll = gpsMap.latlng(); if (!ll) return alert('pick a point');
    const start = document.getElementById('start-utc').value + ':00';
    const body = {
      rinex_path: 'AUTO', lat: ll.lat, lon: ll.lng, alt: 100, start_utc: start,
      duration_s: Number(document.getElementById('duration').value),
      sample_rate: Number(document.getElementById('rate').value),
      sample_format: document.getElementById('fmt').value,
    };
    fetch('/api/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(r => {
      const rd = r.body.getReader(), dec = new TextDecoder();
      (function pump() {
        rd.read().then(({ value, done }) => {
          if (done) return;
          dec.decode(value).split('\n\n').forEach(chunk => {
            const line = chunk.replace(/^data: /, '').trim(); if (!line) return;
            const msg = JSON.parse(line);
            if (msg.progress !== undefined)
              document.getElementById('gen-progress').value = msg.progress;
            if (msg.done) {
              lastOutdir = msg.done.outdir;
              drawInspectTable(msg.done.inspect);
              const a = document.getElementById('download-link');
              a.href = `/static/../out/${msg.done.outdir}/gpssim.bin`; a.hidden = false;
            }
          });
          pump();
        });
      })();
    });
  };

  document.getElementById('btn-receiver').onclick = async () => {
    if (!lastOutdir) return alert('generate first');
    const ll = gpsMap.latlng();
    const r = await fetch('/api/receiver', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ outdir: lastOutdir, marker: [ll.lat, ll.lng, 100] }),
    });
    document.getElementById('fix-readout').textContent = JSON.stringify(await r.json(), null, 1);
  };

  document.getElementById('btn-lnav').onclick = async () => {
    if (!lastOutdir) return alert('generate first');
    const prn = document.getElementById('lnav-prn').value;
    const r = await fetch(`/api/lnav?prn=${prn}&outdir=${lastOutdir}`);
    document.getElementById('lnav-out').textContent = JSON.stringify(await r.json(), null, 1);
  };

  document.getElementById('btn-transmit').onclick = () => {
    if (!lastOutdir) return alert('generate first');
    startTransmit(lastOutdir);
  };
});
