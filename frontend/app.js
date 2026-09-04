// frontend/app.js
let lastOutdir = null;
window.addEventListener('DOMContentLoaded', () => {
  gpsMap.init();

  document.getElementById('btn-preview').onclick = async () => {
    const ll = gpsMap.latlng(); if (!ll) return alert('pick a point');
    const su = document.getElementById('start-utc').value;
    if (!su) return alert('set a start UTC');
    const warn = document.getElementById('warnings');
    warn.textContent = 'loading…';
    let r, txt, d;
    try {
      r = await fetch('/api/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          lat: ll.lat, lon: ll.lng, alt: 100, start_utc: su + ':00',
          rinex_path: document.getElementById('rinex-path').value.trim(),
        }),
      });
      txt = await r.text();
    } catch (e) { warn.textContent = 'request failed: ' + e; return; }
    try { d = JSON.parse(txt); }
    catch (e) { warn.textContent = 'server error ' + r.status + ': ' + txt.slice(0, 300); return; }
    if (!r.ok) { warn.textContent = 'error ' + r.status + ': ' + (d.detail || JSON.stringify(d)); return; }
    if (!d.satellites || !d.satellites.length) { warn.textContent = 'no satellites returned'; return; }
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
    const su = document.getElementById('start-utc').value;
    if (!su) return alert('set a start UTC');
    const start = su + ':00';
    const body = {
      rinex_path: document.getElementById('rinex-path').value.trim() || 'AUTO',
      lat: ll.lat, lon: ll.lng, alt: 100, start_utc: start,
      duration_s: Number(document.getElementById('duration').value),
      sample_rate: Number(document.getElementById('rate').value),
      sample_format: document.getElementById('fmt').value,
    };
    const warn = document.getElementById('warnings');
    warn.textContent = 'generating…';
    fetch('/api/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(async r => {
      if (!r.ok) {
        const t = await r.text();
        let m; try { m = JSON.parse(t).detail; } catch (e) { m = t.slice(0, 300); }
        warn.textContent = 'generate error ' + r.status + ': ' + m;
        return;
      }
      const rd = r.body.getReader(), dec = new TextDecoder();
      (function pump() {
        rd.read().then(({ value, done }) => {
          if (done) { if (warn.textContent === 'generating…') warn.textContent = ''; return; }
          dec.decode(value).split('\n\n').forEach(chunk => {
            const line = chunk.replace(/^data: /, '').trim(); if (!line) return;
            let msg; try { msg = JSON.parse(line); } catch (e) { return; }
            if (msg.progress !== undefined)
              document.getElementById('gen-progress').value = msg.progress;
            if (msg.done) {
              lastOutdir = msg.done.outdir;
              warn.textContent = 'IQ ready: ' + msg.done.outdir;
              drawInspectTable(msg.done.inspect);
              const a = document.getElementById('download-link');
              a.href = `/out/${msg.done.outdir}/gpssim.bin`; a.hidden = false;
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
