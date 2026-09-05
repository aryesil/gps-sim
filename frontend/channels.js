// frontend/channels.js
let _channelCount = 0;
const _channels = {};   // id -> {lastOutdir, lastSatellites, trackFrames, txSlot}

window.channelState = function (id) { return _channels[id]; };

window.addChannel = function () {
  if (Object.keys(_channels).length >= 2) {
    alert('Max 2 channels (PlutoSDR TX1/TX2).');
    return null;
  }
  const id = 'ch' + (++_channelCount);
  _channels[id] = { lastOutdir: null, lastSatellites: null, trackFrames: null, txSlot: null, timeline: [] };

  const card = document.createElement('div');
  card.className = 'channel-card';
  card.id = id;
  card.innerHTML = `
    <div class="channel-header">
      <strong>GPS L1 C/A — Channel ${_channelCount}</strong>
      <span class="badge" id="${id}-badge">STOPPED</span>
      <button id="${id}-start" class="btn-danger">Start</button>
      <button id="${id}-stop">Stop</button>
      <button id="${id}-remove">Remove</button>
    </div>
    <div class="channel-top">
      <div class="col-hw">
        <h4>Hardware Config</h4>
        <label>Device URI <input id="${id}-uri" value="ip:192.168.2.1"></label>
        <label>LO Hz <input id="${id}-lo" value="1575420000"></label>
        <label>TX gain dB <input id="${id}-gain" type="number" value="-50"></label>
        <label><input type="checkbox" id="${id}-tx-dryrun"> Dry run (no RF)</label>
        <label>Auto-stop after (s) <input type="number" id="${id}-max-duration" placeholder="none" min="1"></label>
        <input type="checkbox" id="${id}-tx-confirm" hidden>
        <div class="hint">Isolated setup confirmed by typing TRANSMIT at Start. Auto-stop is a fail-safe: leave blank to run until stopped manually.</div>
      </div>
      <div class="col-sim">
        <h4>Simulation Config</h4>
        <label>Start UTC <input type="datetime-local" id="${id}-start-utc"></label>
        <label>Duration s <input type="number" id="${id}-duration" value="300"></label>
        <label>Sample rate <select id="${id}-rate">
          <option>2600000</option><option>4000000</option><option>5000000</option><option>8000000</option>
        </select></label>
        <label>Format <select id="${id}-fmt"><option>int16</option><option>int8</option></select></label>
        <label>RINEX <input id="${id}-rinex-path" value="AUTO" size="26"></label>
        <div id="${id}-size-estimate" class="hint"></div>
        <div class="scenario-lib-row">
          <input id="${id}-scenario-name" placeholder="scenario name" size="14">
          <button id="${id}-scenario-save">Save</button>
          <select id="${id}-scenario-load"><option value="">Load saved…</option></select>
        </div>
      </div>
      <div class="col-map">
        <h4>Reference Position</h4>
        <div id="${id}-map"></div>
      </div>
    </div>
    <div class="channel-actions">
      <button id="${id}-btn-preview">Preview geometry</button>
      <button id="${id}-btn-generate" class="btn-primary">Generate</button>
      <progress id="${id}-gen-progress" max="1" value="0"></progress>
    </div>
    <div class="timeline-editor">
      <h4>Timeline (scheduled during live transmit)</h4>
      <ul id="${id}-timeline-list"></ul>
      <div class="timeline-add-row">
        <label>at t+ <input id="${id}-tl-at" type="number" value="0" min="0" size="4">s</label>
        <select id="${id}-tl-action">
          <option value="jog">jog</option>
          <option value="time_shift">time_shift</option>
        </select>
        <input id="${id}-tl-arg1" placeholder="direction / field" size="14" value="north">
        <input id="${id}-tl-arg2" type="number" placeholder="distance_m / delta" value="10" size="8">
        <button id="${id}-tl-add">Add step</button>
      </div>
      <div class="hint">Runs only while a live transmit is active on this channel (Start).</div>
    </div>
    <div class="channel-columns">
      <div class="col-panel">
        <div class="channel-tabs">
          <button class="tab-btn active" data-tab="status">Status</button>
          <button class="tab-btn" data-tab="satellites">Satellites</button>
          <button class="tab-btn" data-tab="position">LLA Manipulation</button>
          <button class="tab-btn" data-tab="time">Time Manipulation</button>
        </div>
        <div class="tab-content" data-tab="status">
          <table id="${id}-inspect-table"></table>
          <canvas id="${id}-iq-correlation" width="400" height="100"></canvas>
          <div class="hint">Scrub through the generated file <span id="${id}-iq-scrub-readout"></span></div>
          <input id="${id}-iq-scrub" type="range" min="0" max="0" value="0" style="width:400px">
          <canvas id="${id}-iq-waveform" width="400" height="120"></canvas>
          <div id="${id}-iq-waveform-readout"></div>
          <canvas id="${id}-iq-constellation" width="180" height="180"></canvas>
          <canvas id="${id}-iq-spectrum" width="400" height="120"></canvas>
          <div id="${id}-iq-spectrum-readout"></div>
          <h4>Live Spectrogram</h4>
          <canvas id="${id}-spectrogram" width="400" height="120"></canvas>
          <div class="hint">Fills while this channel is transmitting live -- one column per ~1s segment.</div>
          <h4>C/N0 Trend<span id="${id}-cn0-readout" class="hint"></span></h4>
          <canvas id="${id}-cn0-trend" width="400" height="100"></canvas>
          <div class="hint">Set "Selected PRN" on the Satellites tab before Start to track it live.</div>
        </div>
        <div class="tab-content" data-tab="satellites" hidden>
          <canvas id="${id}-skyplot" width="260" height="260"></canvas>
          <label>Selected PRN <input id="${id}-lnav-prn" type="number" min="1" max="32"></label>
          <div id="${id}-dop"></div>
          <table id="${id}-sat-table"></table>
        </div>
        <div class="tab-content" data-tab="position" hidden>
          <div id="${id}-live-hint" class="hint">Start live transmit to enable jog controls.</div>
          <div id="${id}-jog-controls" hidden>
            <div class="jog-row">
              <div>
                <div class="jog-grid">
                  <span></span><button data-dir="north">N</button><span></span>
                  <button data-dir="west">W</button><span></span><button data-dir="east">E</button>
                  <span></span><button data-dir="south">S</button><span></span>
                </div>
              </div>
              <div class="jog-updown">
                <button data-dir="up">Up</button>
                <button data-dir="down">Down</button>
              </div>
              <div>
                <h4>Offsets</h4>
                <label>Distance step (m) <input id="${id}-jog-step" type="number" value="1000"></label>
                <div id="${id}-live-llh" class="jog-readout"></div>
              </div>
            </div>
          </div>
        </div>
        <div class="tab-content" data-tab="time" hidden>
          <div id="${id}-time-controls" hidden>
            <h4>Time Manipulation</h4>
            <label>GPS Time of Week shift, s</label>
            <div class="time-stepper">
              <button data-field="time_offset_s" data-delta="-30">«</button>
              <button data-field="time_offset_s" data-delta="-1">‹</button>
              <output id="${id}-time-offset">+0</output>
              <button data-field="time_offset_s" data-delta="1">›</button>
              <button data-field="time_offset_s" data-delta="30">»</button>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div id="${id}-warnings" class="warn"></div>
  `;
  document.getElementById('channel-list').appendChild(card);

  card.querySelectorAll('.tab-btn').forEach(btn => {
    btn.onclick = () => {
      card.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      card.querySelectorAll('.tab-content').forEach(c => { c.hidden = c.dataset.tab !== btn.dataset.tab; });
      btn.classList.add('active');
    };
  });
  document.getElementById(`${id}-remove`).onclick = () => {
    delete _channels[id];
    card.remove();
  };

  attachSkyplotClickHandler(`${id}-skyplot`, `${id}-lnav-prn`);
  attachWaveformHover(`${id}-iq-waveform`, `${id}-iq-waveform-readout`);
  attachSpectrumHover(`${id}-iq-spectrum`, `${id}-iq-spectrum-readout`);

  wireChannelActions(id);   // defined in Step 2
  return id;
};

// appended to frontend/channels.js
function wireChannelActions(id) {
  const st = _channels[id];

  function _updateSizeEstimate() {
    const bytesPerSample = document.getElementById(`${id}-fmt`).value === 'int8' ? 1 : 2;
    const rate = Number(document.getElementById(`${id}-rate`).value);
    const duration = Number(document.getElementById(`${id}-duration`).value);
    const bytes = 2 * bytesPerSample * rate * duration;
    document.getElementById(`${id}-size-estimate`).textContent =
      `estimated size: ${(bytes / 1e6).toFixed(1)} MB`;
  }
  [`${id}-fmt`, `${id}-rate`, `${id}-duration`].forEach(elId => {
    document.getElementById(elId).addEventListener('input', _updateSizeEstimate);
    document.getElementById(elId).addEventListener('change', _updateSizeEstimate);
  });
  _updateSizeEstimate();

  // Scenario library: save/load this channel's whole config by name, the
  // same file-per-name pattern as trajectory save/load (backend/scenario_lib.py).
  async function _refreshScenarioOptions() {
    const sel = document.getElementById(`${id}-scenario-load`);
    const r = await fetch('/api/scenario/list');
    if (!r.ok) return;
    const d = await r.json();
    sel.innerHTML = '<option value="">Load saved…</option>' +
      d.names.map(n => `<option value="${n}">${n}</option>`).join('');
  }
  _refreshScenarioOptions();

  document.getElementById(`${id}-scenario-save`).onclick = async () => {
    const name = document.getElementById(`${id}-scenario-name`).value.trim();
    if (!name) return alert('enter a scenario name first');
    const ll = st.map.latlng();
    const params = {
      lat: ll ? ll.lat : 0, lon: ll ? ll.lng : 0, alt: 100,
      start_utc: document.getElementById(`${id}-start-utc`).value + ':00',
      duration_s: Number(document.getElementById(`${id}-duration`).value),
      sample_rate: Number(document.getElementById(`${id}-rate`).value),
      sample_format: document.getElementById(`${id}-fmt`).value,
      rinex_path: document.getElementById(`${id}-rinex-path`).value.trim() || 'AUTO',
      lo_hz: Number(document.getElementById(`${id}-lo`).value),
      tx_gain_db: Number(document.getElementById(`${id}-gain`).value),
    };
    const r = await fetch('/api/scenario/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, params }),
    });
    if (!r.ok) { const d = await r.json(); return alert('save failed: ' + (d.detail || '')); }
    logLine(`Channel ${id}: saved scenario "${name}"`, 'info');
    _refreshScenarioOptions();
  };

  document.getElementById(`${id}-scenario-load`).onchange = async (ev) => {
    const name = ev.target.value;
    if (!name) return;
    const r = await fetch(`/api/scenario/load?name=${encodeURIComponent(name)}`);
    if (!r.ok) { const d = await r.json(); return alert('load failed: ' + (d.detail || '')); }
    const { params } = await r.json();
    if (params.start_utc) document.getElementById(`${id}-start-utc`).value = params.start_utc.slice(0, 16);
    if (params.duration_s !== undefined) document.getElementById(`${id}-duration`).value = params.duration_s;
    if (params.sample_rate !== undefined) document.getElementById(`${id}-rate`).value = params.sample_rate;
    if (params.sample_format) document.getElementById(`${id}-fmt`).value = params.sample_format;
    if (params.rinex_path) document.getElementById(`${id}-rinex-path`).value = params.rinex_path;
    if (params.lo_hz !== undefined) document.getElementById(`${id}-lo`).value = params.lo_hz;
    if (params.tx_gain_db !== undefined) document.getElementById(`${id}-gain`).value = params.tx_gain_db;
    if (params.lat !== undefined && params.lon !== undefined) st.map.setLatlng(params.lat, params.lon);
    _updateSizeEstimate();
    logLine(`Channel ${id}: loaded scenario "${name}"`, 'info');
  };

  function _renderTimeline() {
    const list = document.getElementById(`${id}-timeline-list`);
    list.innerHTML = '';
    st.timeline.forEach((step, i) => {
      const li = document.createElement('li');
      const desc = step.action === 'jog'
        ? `jog ${step.direction} ${step.distance_m}m`
        : `time_shift ${step.field} ${step.delta >= 0 ? '+' : ''}${step.delta}`;
      li.textContent = `t+${step.at_s}s: ${desc} `;
      const rm = document.createElement('button');
      rm.textContent = 'x';
      rm.onclick = () => { st.timeline.splice(i, 1); _renderTimeline(); };
      li.appendChild(rm);
      list.appendChild(li);
    });
  }

  document.getElementById(`${id}-tl-add`).onclick = () => {
    const at_s = Number(document.getElementById(`${id}-tl-at`).value);
    const action = document.getElementById(`${id}-tl-action`).value;
    const arg1 = document.getElementById(`${id}-tl-arg1`).value.trim();
    const arg2 = Number(document.getElementById(`${id}-tl-arg2`).value);
    const step = action === 'jog'
      ? { at_s, action, direction: arg1, distance_m: arg2 }
      : { at_s, action, field: arg1, delta: arg2 };
    st.timeline.push(step);
    _renderTimeline();
  };

  document.getElementById(`${id}-btn-preview`).onclick = async () => {
    const su = document.getElementById(`${id}-start-utc`).value;
    if (!su) return alert('set a start UTC');
    const warn = document.getElementById(`${id}-warnings`);
    warn.textContent = 'loading…';
    const r = await fetch('/api/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lat: st.map.latlng() ? st.map.latlng().lat : 0,
        lon: st.map.latlng() ? st.map.latlng().lng : 0,
        alt: 100, start_utc: su + ':00',
        rinex_path: document.getElementById(`${id}-rinex-path`).value.trim(),
      }),
    });
    const d = await r.json();
    if (!r.ok) { warn.textContent = 'error: ' + (d.detail || JSON.stringify(d)); logLine('Channel ' + id + ' preview: ' + (d.detail || JSON.stringify(d)), 'error'); return; }
    st.lastSatellites = d.satellites;
    drawSkyplot(`${id}-skyplot`, d.satellites);
    drawDop(`${id}-dop`, d.dop);
    drawSatTable(`${id}-sat-table`, d.satellites);
    document.getElementById(`${id}-warnings`).textContent = d.warnings.join(' · ');
  };

  document.getElementById(`${id}-btn-generate`).onclick = () => {
    const su = document.getElementById(`${id}-start-utc`).value;
    if (!su) return alert('set a start UTC');
    const body = {
      rinex_path: document.getElementById(`${id}-rinex-path`).value.trim() || 'AUTO',
      lat: st.map.latlng() ? st.map.latlng().lat : 0,
      lon: st.map.latlng() ? st.map.latlng().lng : 0,
      alt: 100, start_utc: su + ':00',
      duration_s: Number(document.getElementById(`${id}-duration`).value),
      sample_rate: Number(document.getElementById(`${id}-rate`).value),
      sample_format: document.getElementById(`${id}-fmt`).value,
    };
    if (st.route) body.route = st.route;
    fetch('/api/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(r => {
      if (!r.ok) {
        r.json().then(d => {
          const msg = 'Channel ' + id + ' generate failed: ' + (d.detail || JSON.stringify(d));
          document.getElementById(`${id}-warnings`).textContent = msg;
          logLine(msg, 'error');
        });
        return;
      }
      const rd = r.body.getReader(), dec = new TextDecoder();
      (function pump() {
        rd.read().then(({ value, done }) => {
          if (done) return;
          dec.decode(value).split('\n\n').forEach(chunk => {
            const line = chunk.replace(/^data: /, '').trim(); if (!line) return;
            const msg = JSON.parse(line);
            if (msg.progress !== undefined)
              document.getElementById(`${id}-gen-progress`).value = msg.progress;
            if (msg.error) {
              document.getElementById(`${id}-warnings`).textContent = 'error: ' + msg.error;
              logLine('Channel ' + id + ': ' + msg.error, 'error');
            }
            if (msg.done) {
              st.lastOutdir = msg.done.outdir;
              drawInspectTable(`${id}-inspect-table`, msg.done.inspect);
              drawCorrelationBars(`${id}-iq-correlation`, msg.done.inspect);
              loadIqPlots(id, msg.done.outdir);
              attachIqScrubber(id, msg.done.outdir);
              if (st.lastSatellites) {
                const cn0 = {}; msg.done.inspect.forEach(r => { cn0[r.prn] = r.metric_db; });
                drawSkyplot(`${id}-skyplot`, st.lastSatellites, cn0);
              }
            }
          });
          pump();
        }).catch(() => {
          logLine('Channel ' + id + ' generate stream error', 'error');
        });
      })();
    });
  };

  st.map = gpsMap.init(`${id}-map`);

  document.getElementById(`${id}-start`).onclick = async () => {
    const ll = st.map.latlng();
    if (!ll) return alert('click the map to set a start position first');
    if (!document.getElementById(`${id}-tx-confirm`).checked) {
      openConfirmModal(() => {
        document.getElementById(`${id}-tx-confirm`).checked = true;
        document.getElementById(`${id}-start`).click();
      });
      return;
    }
    const body = {
      rinex_path: document.getElementById(`${id}-rinex-path`).value.trim() || 'AUTO',
      lat: ll.lat, lon: ll.lng, alt: 100,
      start_utc: document.getElementById(`${id}-start-utc`).value + ':00',
      duration_s: Number(document.getElementById(`${id}-duration`).value),
      sample_rate: Number(document.getElementById(`${id}-rate`).value),
      sample_format: document.getElementById(`${id}-fmt`).value,
      uri: document.getElementById(`${id}-uri`).value,
      lo_hz: Number(document.getElementById(`${id}-lo`).value),
      tx_gain_db: Number(document.getElementById(`${id}-gain`).value),
      confirm_isolated: document.getElementById(`${id}-tx-confirm`).checked,
      dry_run: document.getElementById(`${id}-tx-dryrun`).checked,
    };
    const prnInput = document.getElementById(`${id}-lnav-prn`);
    if (prnInput && prnInput.value) body.track_prn = Number(prnInput.value);
    const maxDurInput = document.getElementById(`${id}-max-duration`);
    if (maxDurInput && maxDurInput.value) body.max_duration_s = Number(maxDurInput.value);
    if (st.timeline.length) body.timeline = st.timeline;
    const r = await fetch('/api/live/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (r.status === 403) { alert('Set ALLOW_TX and tick the isolated-setup confirmation.'); return; }
    if (r.status === 409) { alert('Both TX1 and TX2 are already transmitting.'); return; }
    if (!r.ok) {
      // Anything else (validation 400, 503 EphemerisUnavailable, 5xx) never
      // produces an SSE stream, so the badge must NOT go to 'On Air' -- it
      // would stay there forever with no 'finished' message to clear it.
      let detail;
      try { const d = await r.json(); detail = d.detail || JSON.stringify(d); }
      catch (e) { detail = 'HTTP ' + r.status; }
      const msg = 'Channel ' + id + ' live start failed: ' + detail;
      document.getElementById(`${id}-warnings`).textContent = msg;
      if (window.logLine) logLine(msg, 'error');
      alert(msg);
      return;
    }
    const badge = document.getElementById(`${id}-badge`);
    badge.textContent = 'LIVE'; badge.classList.add('badge-live');
    const rd = r.body.getReader(), dec = new TextDecoder();
    (function pump() {
      rd.read().then(({ value, done }) => {
        if (done) return;
        dec.decode(value).split('\n\n').forEach(chunk => {
          const line = chunk.replace(/^data: /, '').trim(); if (!line) return;
          const msg = JSON.parse(line);
          if (msg.slot && !channelState(id).txSlot) enableLiveTabs(id, msg.slot);
          if (msg.spectrogram_db) pushSpectrogramColumn(`${id}-spectrogram`, msg.spectrogram_db);
          if (msg.cn0_db !== undefined) pushCn0Sample(`${id}-cn0-trend`, msg.cn0_db);
          if (msg.timeline_step) logLine(`Channel ${id} timeline: ${JSON.stringify(msg.timeline_step)}`, 'info');
          if (msg.finished) { badge.textContent = 'STOPPED'; badge.classList.remove('badge-live'); disableLiveTabs(id); }
        });
        pump();
      }).catch(() => {
        badge.textContent = 'STOPPED'; badge.classList.remove('badge-live');
        disableLiveTabs(id);
      });
    })();
  };

  document.getElementById(`${id}-stop`).onclick = () => {
    const slot = channelState(id).txSlot;
    if (slot) fetch('/api/live/stop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slot }),
    });
  };
}
