// frontend/app.js
let lastOutdir = null;
let lastSatellites = null;
let trackFrames = null;
window.addEventListener('DOMContentLoaded', () => {
  gpsMap.init();

  // Live estimated .bin size, mirroring backend/scenario.py:estimate_bytes
  // (2 IQ channels * bytes-per-sample * sample_rate * duration_s).
  function _updateSizeEstimate() {
    const bytesPerSample = document.getElementById('fmt').value === 'int8' ? 1 : 2;
    const rate = Number(document.getElementById('rate').value);
    const duration = Number(document.getElementById('duration').value);
    const bytes = 2 * bytesPerSample * rate * duration;
    document.getElementById('size-estimate').textContent =
      `estimated size: ${(bytes / 1e6).toFixed(1)} MB`;
  }
  ['fmt', 'rate', 'duration'].forEach(id => {
    document.getElementById(id).addEventListener('input', _updateSizeEstimate);
    document.getElementById(id).addEventListener('change', _updateSizeEstimate);
  });
  _updateSizeEstimate();

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
    lastSatellites = d.satellites;
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
            if (msg.error) { warn.textContent = 'generate error: ' + msg.error; return; }
            if (msg.done) {
              lastOutdir = msg.done.outdir;
              warn.textContent = 'IQ ready: ' + msg.done.outdir;
              drawInspectTable(msg.done.inspect);
              drawCorrelationBars(msg.done.inspect);
              loadIqPlots(msg.done.outdir);
              if (lastSatellites) {
                const cn0 = {};
                msg.done.inspect.forEach(r => { cn0[r.prn] = r.metric_db; });
                drawSkyplot(lastSatellites, cn0);
              }
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
    const out = document.getElementById('lnav-out');
    let r, txt;
    try {
      r = await fetch(`/api/lnav?prn=${prn}&outdir=${lastOutdir}`);
      txt = await r.text();
    } catch (e) { out.textContent = 'request failed: ' + e; return; }
    if (!r.ok) {
      let m; try { m = JSON.parse(txt).detail; } catch (e) { m = txt.slice(0, 300); }
      out.textContent = 'error ' + r.status + ': ' + m;
      return;
    }
    out.textContent = JSON.stringify(JSON.parse(txt), null, 1);
  };

  document.getElementById('btn-corr-curve').onclick = () => {
    if (!lastOutdir) return alert('generate first');
    const prn = document.getElementById('lnav-prn').value;
    loadCorrelationCurve(lastOutdir, prn);
  };

  document.getElementById('btn-track').onclick = async () => {
    const ll = gpsMap.latlng(); if (!ll) return alert('pick a point');
    const su = document.getElementById('start-utc').value;
    if (!su) return alert('set a start UTC');
    const warn = document.getElementById('warnings');
    const r = await fetch('/api/preview_track', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lat: ll.lat, lon: ll.lng, alt: 100, start_utc: su + ':00',
        duration_s: Number(document.getElementById('duration').value),
        step_s: Number(document.getElementById('track-step').value) || 30,
        rinex_path: document.getElementById('rinex-path').value.trim(),
      }),
    });
    const d = await r.json();
    if (!r.ok) { warn.textContent = 'track error: ' + (d.detail || JSON.stringify(d)); return; }
    trackFrames = d.frames;
    const slider = document.getElementById('track-slider');
    slider.max = trackFrames.length - 1;
    slider.value = 0;
    slider.disabled = trackFrames.length < 2;
    document.getElementById('btn-track-play').disabled = trackFrames.length < 2;
    _drawTrackFrame(0);
  };

  document.getElementById('track-slider').oninput = (ev) => _drawTrackFrame(Number(ev.target.value));

  let _trackTimer = null;
  document.getElementById('btn-track-play').onclick = (ev) => {
    const btn = ev.target;
    if (_trackTimer) {
      clearInterval(_trackTimer); _trackTimer = null; btn.textContent = 'Play';
      return;
    }
    btn.textContent = 'Pause';
    const slider = document.getElementById('track-slider');
    _trackTimer = setInterval(() => {
      let v = Number(slider.value) + 1;
      if (v > Number(slider.max)) v = 0;
      slider.value = v;
      _drawTrackFrame(v);
    }, 500);
  };

  function _drawTrackFrame(idx) {
    if (!trackFrames || !trackFrames[idx]) return;
    const f = trackFrames[idx];
    lastSatellites = f.satellites;
    drawSkyplot(f.satellites);
    document.getElementById('track-readout').textContent =
      `t+${f.t_offset_s}s -- ${f.satellites.length} visible`;
  }

  document.getElementById('btn-transmit').onclick = () => {
    if (!lastOutdir) return alert('generate first');
    startTransmit(lastOutdir);
  };
});
