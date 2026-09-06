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
        <div class="device-row">
          <button id="${id}-dev-connect" class="btn-secondary">Connect</button>
          <span id="${id}-dev-status" class="dev-status dev-off">not connected</span>
        </div>
        <label>LO Hz <input id="${id}-lo" value="1575420000"></label>
        <label>TX gain dB <input id="${id}-gain" type="number" value="-50"></label>
        <label><input type="checkbox" id="${id}-tx-dryrun"> Dry run (no RF)</label>
        <label>Auto-stop after (s) <input type="number" id="${id}-max-duration" placeholder="none" min="1"><span class="info" title="Fail-safe: leave blank to run until stopped manually. The isolated-setup confirmation is done by typing TRANSMIT at Start.">i</span></label>
        <label><input type="checkbox" id="${id}-record"> Record this session</label>
        <div class="scenario-lib-row">
          <select id="${id}-replay-select"><option value="">Replay recording…</option></select>
          <button id="${id}-replay-play">Play</button>
        </div>
        <input type="checkbox" id="${id}-tx-confirm" hidden>
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
        <label>Ephemeris
          <select id="${id}-eph-mode">
            <option value="broadcast">Broadcast (realigned)</option>
            <option value="precise">Precise (SP3-fitted)</option>
          </select>
          <span class="info" title="precise: SP3 orbit/clock fitted into the broadcast records that drive generation. Preview/Generate auto-download the best free IGS product (rapid, then final) for the Start UTC — no file to place. Rapid lags ~17 h, final ~12 d; very recent epochs may have no product yet.">i</span>
        </label>
        <details class="precise-panel">
          <summary>Precise ephemeris (SP3) — optional manual override <span class="info" title="Only needed to pin a specific local SP3 instead of the auto-downloaded one.">i</span></summary>
          <label>SP3 path <input id="${id}-sp3-path" size="26" placeholder="(leave blank — auto-downloaded)"></label>
          <button id="${id}-sp3-load" type="button">Load this file</button>
          <button id="${id}-sp3-compare" type="button">Compare vs broadcast</button>
          <div id="${id}-sp3-status" class="hint"></div>
        </details>
        <details class="impairments-panel">
          <summary>RF impairments (advanced) <span class="info" title="Deterministic, seeded. Post-processes the gps-sdr-sim output; a clean copy is kept as gpssim.clean.bin. All-zero fields = no-op.">i</span></summary>
          <label>Preset <select id="${id}-imp-preset">
            <option value="manual">Custom (enter values below)</option>
            <option value="bench">Bench cable test (near-ideal)</option>
            <option value="field">Field test — typical SDR + antenna</option>
            <option value="urban">Field test — degraded / urban multipath</option>
          </select><span class="info" title="Picking a preset fills every field below with representative SDR-replay values and ticks the enable box. Editing any field afterwards drops the preset back to Custom. Values are illustrative starting points, not a calibrated device model.">i</span></label>
          <label><input type="checkbox" id="${id}-imp-enabled"> Apply impairments to generated IQ</label>
          <label>Seed <input id="${id}-imp-seed" type="number" value="0"></label>
          <label>Carrier freq offset Hz <input id="${id}-imp-cfo" type="number" value="0"></label>
          <label>Sample-rate error ppm <input id="${id}-imp-ppm" type="number" value="0"></label>
          <label>Phase noise deg RMS <input id="${id}-imp-phn" type="number" value="0" min="0"></label>
          <label>I/Q gain imbalance dB <input id="${id}-imp-gain" type="number" value="0"></label>
          <label>I/Q phase error deg <input id="${id}-imp-iqphase" type="number" value="0"></label>
          <label>DC offset I <input id="${id}-imp-dci" type="number" value="0"></label>
          <label>DC offset Q <input id="${id}-imp-dcq" type="number" value="0"></label>
          <label>Target SNR dB <input id="${id}-imp-snr" type="number" placeholder="none"></label>
          <label>Clip fraction <input id="${id}-imp-clip" type="number" value="0" min="0" max="1" step="0.001"></label>
          <label>Quantizer bits <input id="${id}-imp-bits" type="number" value="0" min="0" max="16"></label>
        </details>
        <details class="models-panel">
          <summary>Propagation &amp; receiver models (advanced) <span class="info" title="Deterministic, RNG-free. These always shape the Preview / truth observables. They alter the generated IQ only when 'apply to IQ' is ticked: ionosphere then rides gps-sdr-sim's own broadcast Klobuchar, and a quasi-static receiver-clock + multipath channel is convolved onto the composite signal (clean copy kept as gpssim.prechannel.bin). Troposphere stays truth-only. All 'off' = no change.">i</span></summary>
          <label>Preset <select id="${id}-mdl-preset">
            <option value="manual">Custom (set fields below)</option>
            <option value="open-sky">Open sky — mid-latitude, clean</option>
            <option value="urban-canyon">Urban canyon — strong multipath + clock sawtooth</option>
            <option value="foliage-weak">Foliage / weak signal — one reflection, drifting clock</option>
          </select><span class="info" title="Fills the model fields with a representative scenario. 'Apply to IQ' is left as you set it. Editing any field reverts to Custom. Illustrative values, not a site-calibrated model.">i</span></label>
          <label>Ionosphere <select id="${id}-mdl-iono">
            <option value="off">off</option><option value="klobuchar">klobuchar</option>
          </select></label>
          <label>Troposphere <select id="${id}-mdl-tropo">
            <option value="off">off</option><option value="saastamoinen">saastamoinen</option>
          </select></label>
          <label>Receiver clock <select id="${id}-mdl-rxclk">
            <option value="off">off</option><option value="poly">poly</option>
          </select></label>
          <label>· bias s <input id="${id}-mdl-rxclk-bias" type="number" value="0" step="1e-7"></label>
          <label>· drift s/s <input id="${id}-mdl-rxclk-drift" type="number" value="0" step="1e-10"></label>
          <label>· drift rate s/s² <input id="${id}-mdl-rxclk-driftrate" type="number" value="0" step="1e-13"></label>
          <label>· sawtooth amp s <input id="${id}-mdl-rxclk-sawamp" type="number" value="0" min="0" step="1e-9"></label>
          <label>· sawtooth period s <input id="${id}-mdl-rxclk-sawper" type="number" value="0" min="0"></label>
          <label>Multipath <select id="${id}-mdl-mp">
            <option value="off">off</option><option value="specular">specular</option>
          </select><span class="info" title="Reflections — a row counts only when amplitude > 0 (0 ≤ amp < 1).">i</span></label>
          <label>· #1 excess delay m <input id="${id}-mdl-mp1-delay" type="number" value="0" min="0"></label>
          <label>· #1 amplitude <input id="${id}-mdl-mp1-amp" type="number" value="0" min="0" max="0.999" step="0.01"></label>
          <label>· #1 phase rad <input id="${id}-mdl-mp1-phase" type="number" value="3.14159" step="0.01"></label>
          <label>· #2 excess delay m <input id="${id}-mdl-mp2-delay" type="number" value="0" min="0"></label>
          <label>· #2 amplitude <input id="${id}-mdl-mp2-amp" type="number" value="0" min="0" max="0.999" step="0.01"></label>
          <label>· #2 phase rad <input id="${id}-mdl-mp2-phase" type="number" value="3.14159" step="0.01"></label>
          <label><input type="checkbox" id="${id}-mdl-to-iq"> Also apply these models to the generated IQ</label>
          <div id="${id}-mdl-summary" class="hint"></div>
        </details>
        <details class="engine-panel"><summary>Signal engine <span class="info" title="gps-sdr-sim is the default external generator (GPS L1 C/A only). native is the built-in C++ engine: same GPS L1 C/A output plus a seeded per-satellite fading model baked into the IQ. Leave on gps-sdr-sim for the unchanged workflow.">i</span></summary>
          <label>Engine <select id="${id}-engine">
            <option value="gps-sdr-sim">gps-sdr-sim (default)</option>
            <option value="native">native (C++ engine, GPS L1 C/A + fading)</option>
          </select></label>
          <fieldset class="sys-set"><legend>Constellations (native)</legend>
            <label><input type="checkbox" id="${id}-sys-G" checked disabled> GPS</label>
            <label><input type="checkbox" id="${id}-sys-R"> GLONASS</label>
            <label><input type="checkbox" id="${id}-sys-E"> Galileo</label>
            <label><input type="checkbox" id="${id}-sys-C"> BeiDou</label>
            <label><input type="checkbox" id="${id}-sys-J"> QZSS</label>
            <label><input type="checkbox" id="${id}-sys-S"> SBAS</label>
          </fieldset>
          <label>Sample rate Hz <input id="${id}-fs" type="number" step="100000" placeholder="auto from signals"></label>
          <label>Quantization <select id="${id}-quant">
            <option value="int16">int16</option>
            <option value="int12">int12</option>
            <option value="int8">int8</option>
          </select></label>
          <label>Fading model <select id="${id}-fade-model">
            <option value="off">off</option>
            <option value="lognormal">log-normal</option>
          </select></label>
          <label>sigma dB <input id="${id}-fade-sigma" type="number" step="0.5" value="3"></label>
          <label>coherence s <input id="${id}-fade-coh" type="number" step="0.5" value="2"></label>
          <label>seed <input id="${id}-fade-seed" type="number" step="1" value="1"></label>
        </details>
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
    <div class="compare-region" id="${id}-compare-region" hidden>
      <div class="compare-region-head">
        <h4>Ephemeris comparison — broadcast vs precise</h4>
        <button id="${id}-compare-close" type="button">Hide</button>
      </div>
      <div id="${id}-sp3-compare-out" class="compare-out"></div>
    </div>
    <div class="channel-actions">
      <button id="${id}-btn-preview">Preview geometry</button>
      <button id="${id}-btn-generate" class="btn-primary">Generate</button>
      <progress id="${id}-gen-progress" max="1" value="0"></progress>
    </div>
    <div class="timeline-editor">
      <h4>Timeline (scheduled during live transmit) <span class="info" title="Runs only while a live transmit is active on this channel (Start).">i</span></h4>
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
          <table id="${id}-svpower-table"></table>
          <div class="iq-inspect">
            <div class="iq-block">
              <div class="iq-label" id="${id}-iq-correlation-label">Acquisition metric</div>
              <canvas id="${id}-iq-correlation" width="760" height="140"></canvas>
            </div>
            <div class="iq-block">
              <div class="iq-label">
                Waveform scrubber
                <span id="${id}-iq-scrub-readout" class="iq-readout"></span>
              </div>
              <input id="${id}-iq-scrub" type="range" min="0" max="0" value="0" class="iq-scrub">
              <canvas id="${id}-iq-waveform" width="760" height="120"></canvas>
              <div id="${id}-iq-waveform-readout" class="iq-readout"></div>
            </div>
            <div class="iq-row">
              <div class="iq-block iq-block-fixed">
                <div class="iq-label">Constellation</div>
                <canvas id="${id}-iq-constellation" width="200" height="200"></canvas>
              </div>
              <div class="iq-block iq-block-grow">
                <div class="iq-label">Spectrum</div>
                <canvas id="${id}-iq-spectrum" width="520" height="200"></canvas>
                <div id="${id}-iq-spectrum-readout" class="iq-readout"></div>
              </div>
            </div>
            <div class="iq-block">
              <div class="iq-label">Live spectrogram <span class="info" title="Fills while this channel is transmitting live — one column per ~1 s segment.">i</span></div>
              <canvas id="${id}-spectrogram" width="760" height="120"></canvas>
            </div>
            <div class="iq-block">
              <div class="iq-label">
                C/N0 trend
                <span class="info" title="Set 'Selected PRN' on the Satellites tab before Start to track it live.">i</span>
                <span id="${id}-cn0-readout" class="iq-readout"></span>
              </div>
              <canvas id="${id}-cn0-trend" width="760" height="110"></canvas>
            </div>
          </div>
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
  if (window.deviceUI) deviceUI.wireChannel(id);
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

  // Engine select: toggle disabled state on non-G constellation checkboxes
  // (they are only available when native engine is selected).
  function _updateEngineConstellationState() {
    const isNative = document.getElementById(`${id}-engine`).value === 'native';
    ['R', 'E', 'C', 'J', 'S'].forEach(s => {
      const box = document.getElementById(`${id}-sys-${s}`);
      box.disabled = !isNative;
      // gps-sdr-sim is GPS-only: also clear the boxes so _engineBody() stops
      // emitting `systems` and /api/generate no longer 422s. Switching back to
      // native only re-enables them (no auto-check).
      if (!isNative) box.checked = false;
    });
  }
  document.getElementById(`${id}-engine`).addEventListener('change', _updateEngineConstellationState);
  _updateEngineConstellationState();

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

  // Session replay: feed a previously recorded session's SSE stream back
  // through the same spectrogram/C-N0/log renderers a live transmit uses.
  // Read-only playback -- never touches _tx_slots or the badge/txSlot
  // live-session state, so it can run even while the channel is idle.
  async function _refreshReplayOptions() {
    const sel = document.getElementById(`${id}-replay-select`);
    const r = await fetch('/api/recording/list');
    if (!r.ok) return;
    const d = await r.json();
    sel.innerHTML = '<option value="">Replay recording…</option>' +
      d.names.map(n => `<option value="${n}">${n}</option>`).join('');
  }
  _refreshReplayOptions();

  document.getElementById(`${id}-replay-play`).onclick = async () => {
    const name = document.getElementById(`${id}-replay-select`).value;
    if (!name) return alert('pick a recording to replay first');
    logLine(`Channel ${id}: replaying "${name}"`, 'info');
    const r = await fetch(`/api/recording/replay?name=${encodeURIComponent(name)}&speed=1`);
    if (!r.ok) return alert('replay failed: HTTP ' + r.status);
    const rd = r.body.getReader(), dec = new TextDecoder();
    (function pump() {
      rd.read().then(({ value, done }) => {
        if (done) return;
        dec.decode(value).split('\n\n').forEach(chunk => {
          const line = chunk.replace(/^data: /, '').trim(); if (!line) return;
          const msg = JSON.parse(line);
          if (msg.spectrogram_db) pushSpectrogramColumn(`${id}-spectrogram`, msg.spectrogram_db);
          if (msg.cn0_db !== undefined) pushCn0Sample(`${id}-cn0-trend`, msg.cn0_db);
          if (msg.timeline_step) logLine(`Channel ${id} replay: ${JSON.stringify(msg.timeline_step)}`, 'info');
          if (msg.finished) logLine(`Channel ${id}: replay "${name}" finished`, 'info');
        });
        pump();
      }).catch(() => {});
    })();
  };

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
        ephemeris_mode: document.getElementById(`${id}-eph-mode`).value,
        systems: ["G", "R", "E", "C", "J", "S"].filter(
          s => (document.getElementById(`${id}-sys-${s}`) || {}).checked),
        ...(_channelModelsBody() || {}),
      }),
    });
    const d = await r.json();
    if (!r.ok) { warn.textContent = 'error: ' + (d.detail || JSON.stringify(d)); logLine('Channel ' + id + ' preview: ' + (d.detail || JSON.stringify(d)), 'error'); return; }
    st.lastSatellites = d.satellites;
    drawSkyplot(`${id}-skyplot`, d.satellites);
    drawDop(`${id}-dop`, d.dop);
    drawSatTable(`${id}-sat-table`, d.satellites);
    _renderModelSummary(d.channel_models);
    document.getElementById(`${id}-warnings`).textContent = d.warnings.join(' · ');
  };

  async function _refreshSp3Status() {
    const el = document.getElementById(`${id}-sp3-status`);
    try {
      const s = await (await fetch('/api/precise/status')).json();
      el.textContent = s.loaded
        ? `loaded: ${s.source} — ${s.satellites.length} sats, ${s.epochs} epochs, ${s.coverage_start_utc} … ${s.coverage_end_utc}`
        : 'no SP3 product loaded';
    } catch (e) { el.textContent = 'status unavailable'; }
  }
  _refreshSp3Status();

  document.getElementById(`${id}-sp3-load`).onclick = async () => {
    const path = document.getElementById(`${id}-sp3-path`).value.trim();
    if (!path) return alert('set an SP3 path');
    const el = document.getElementById(`${id}-sp3-status`);
    el.textContent = 'loading…';
    const r = await fetch('/api/precise/load', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const d = await r.json();
    if (!r.ok) { el.textContent = 'error: ' + (d.detail || JSON.stringify(d)); return; }
    logLine('Channel ' + id + ' loaded SP3: ' + d.source, 'info');
    _refreshSp3Status();
  };

  document.getElementById(`${id}-compare-close`).onclick = () => {
    document.getElementById(`${id}-compare-region`).hidden = true;
  };

  document.getElementById(`${id}-sp3-compare`).onclick = async () => {
    const su = document.getElementById(`${id}-start-utc`).value;
    if (!su) return alert('set a start UTC');
    const out = document.getElementById(`${id}-sp3-compare-out`);
    const region = document.getElementById(`${id}-compare-region`);
    // Sweep across the scenario duration so the result is a curve, not a
    // single-epoch snapshot; ~20 steps, at least 60 s apart.
    const dur = Math.max(0, Number(document.getElementById(`${id}-duration`).value) || 0);
    const step = Math.max(60, Math.round(dur / 20) || 60);
    const reqBody = {
      lat: st.map.latlng() ? st.map.latlng().lat : 0,
      lon: st.map.latlng() ? st.map.latlng().lng : 0,
      alt: 100, start_utc: su + ':00',
      rinex_path: document.getElementById(`${id}-rinex-path`).value.trim(),
      sweep_s: dur, step_s: step,
    };
    // Re-open from memory when nothing that affects the result changed.
    const key = JSON.stringify(reqBody);
    if (st._cmpKey === key && st._cmpData) {
      region.hidden = false;
      renderCompare(`${id}-sp3-compare-out`, st._cmpData);
      return;
    }
    region.hidden = false;
    out.className = 'compare-out';
    out.textContent = 'comparing…';
    const r = await fetch('/api/precise/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reqBody),
    });
    const d = await r.json();
    if (!r.ok) { out.textContent = 'error: ' + (d.detail || JSON.stringify(d)); return; }
    st._cmpKey = key;
    st._cmpData = d;
    renderCompare(`${id}-sp3-compare-out`, d);
  };

  // Fold the advanced RF-impairment panel into an `impairments` object only
  // when the operator ticks the enable box. Untouched panel -> null -> the
  // /api/generate body is byte-identical to before this panel existed.
  // The raw AWGN-power knob is deliberately not exposed (it is mutually
  // exclusive with snr_db server-side); leave the SNR field blank for no AWGN.
  // Field-test impairment presets. Representative wideband SDR-replay
  // values (PlutoSDR-class front end + antenna), not a calibrated device
  // model -- a starting point the operator then tunes. Selecting one fills
  // the fields and enables the panel; hand-editing any field reverts the
  // selector to "manual".
  const IMP_PRESETS = {
    bench: { seed: 1, cfo: 20, ppm: 0.1, phn: 0.3, gain: 0.05, iqphase: 0.3,
             dci: 0.001, dcq: 0.001, snr: 40, clip: 0, bits: 12 },
    field: { seed: 7, cfo: 250, ppm: 1.0, phn: 1.5, gain: 0.3, iqphase: 1.5,
             dci: 0.008, dcq: 0.008, snr: 12, clip: 0.02, bits: 10 },
    urban: { seed: 13, cfo: 600, ppm: 2.5, phn: 3.0, gain: 0.6, iqphase: 3.0,
             dci: 0.02, dcq: 0.02, snr: 4, clip: 0.05, bits: 8 },
  };
  const _impPresetSel = document.getElementById(`${id}-imp-preset`);
  _impPresetSel.onchange = () => {
    const p = IMP_PRESETS[_impPresetSel.value];
    if (!p) return;
    document.getElementById(`${id}-imp-enabled`).checked = true;
    for (const [sfx, v] of Object.entries(p)) {
      const f = document.getElementById(`${id}-imp-${sfx}`);
      if (f) f.value = v;
    }
  };
  ['seed', 'cfo', 'ppm', 'phn', 'gain', 'iqphase', 'dci', 'dcq', 'snr', 'clip', 'bits']
    .forEach(sfx => document.getElementById(`${id}-imp-${sfx}`)
      .addEventListener('input', () => { _impPresetSel.value = 'manual'; }));

  function _impairmentsBody() {
    if (!document.getElementById(`${id}-imp-enabled`).checked) return null;
    const num = (sfx) => Number(document.getElementById(`${id}-imp-${sfx}`).value) || 0;
    const imp = {
      enabled_flag: true,
      seed: num('seed'),
      cfo_hz: num('cfo'),
      sample_rate_ppm: num('ppm'),
      phase_noise_deg_rms: num('phn'),
      iq_gain_db: num('gain'),
      iq_phase_deg: num('iqphase'),
      dc_i: num('dci'),
      dc_q: num('dcq'),
      clip_fraction: num('clip'),
      quant_bits: num('bits'),
    };
    const snrRaw = document.getElementById(`${id}-imp-snr`).value.trim();
    if (snrRaw !== '') imp.snr_db = Number(snrRaw);
    return imp;
  }

  // Fold the propagation / receiver-model panel into the request body.
  // Every sub-model defaults to "off"; the whole object is omitted when
  // nothing is enabled, so an untouched panel leaves the request as it was.
  // Propagation / receiver-model presets. Representative field scenarios,
  // not site-calibrated models -- a starting point the operator tunes.
  // Selecting one fills the fields; "apply to IQ" is left untouched;
  // hand-editing any field reverts the selector to "manual".
  const MDL_PRESETS = {
    'open-sky': {
      iono: 'klobuchar', tropo: 'saastamoinen', rxclk: 'poly',
      'rxclk-bias': 1e-7, 'rxclk-drift': 1e-9, 'rxclk-driftrate': 0,
      'rxclk-sawamp': 0, 'rxclk-sawper': 0, mp: 'off',
      'mp1-delay': 0, 'mp1-amp': 0, 'mp1-phase': 3.14159,
      'mp2-delay': 0, 'mp2-amp': 0, 'mp2-phase': 3.14159,
    },
    'urban-canyon': {
      iono: 'klobuchar', tropo: 'saastamoinen', rxclk: 'poly',
      'rxclk-bias': 5e-7, 'rxclk-drift': 2e-9, 'rxclk-driftrate': 1e-12,
      'rxclk-sawamp': 5e-9, 'rxclk-sawper': 1.0, mp: 'specular',
      'mp1-delay': 15, 'mp1-amp': 0.5, 'mp1-phase': 3.14159,
      'mp2-delay': 45, 'mp2-amp': 0.3, 'mp2-phase': 3.14159,
    },
    'foliage-weak': {
      iono: 'klobuchar', tropo: 'saastamoinen', rxclk: 'poly',
      'rxclk-bias': 2e-7, 'rxclk-drift': 5e-9, 'rxclk-driftrate': 0,
      'rxclk-sawamp': 0, 'rxclk-sawper': 0, mp: 'specular',
      'mp1-delay': 8, 'mp1-amp': 0.35, 'mp1-phase': 3.14159,
      'mp2-delay': 0, 'mp2-amp': 0, 'mp2-phase': 3.14159,
    },
  };
  const _mdlPresetSel = document.getElementById(`${id}-mdl-preset`);
  _mdlPresetSel.onchange = () => {
    const p = MDL_PRESETS[_mdlPresetSel.value];
    if (!p) return;
    for (const [sfx, v] of Object.entries(p)) {
      const f = document.getElementById(`${id}-mdl-${sfx}`);
      if (f) f.value = v;
    }
  };
  ['iono', 'tropo', 'rxclk', 'rxclk-bias', 'rxclk-drift', 'rxclk-driftrate',
   'rxclk-sawamp', 'rxclk-sawper', 'mp', 'mp1-delay', 'mp1-amp', 'mp1-phase',
   'mp2-delay', 'mp2-amp', 'mp2-phase', 'to-iq'].forEach(sfx => {
    const f = document.getElementById(`${id}-mdl-${sfx}`);
    ['input', 'change'].forEach(ev => f.addEventListener(ev, () => {
      _mdlPresetSel.value = 'manual';
    }));
  });

  function _channelModelsBody() {
    const val = (sfx) => document.getElementById(`${id}-mdl-${sfx}`).value;
    const num = (sfx) => Number(document.getElementById(`${id}-mdl-${sfx}`).value) || 0;
    const out = {};
    const iono = val('iono'), tropo = val('tropo');
    if (iono !== 'off' || tropo !== 'off') out.atmosphere = { ionosphere: iono, troposphere: tropo };
    if (val('rxclk') === 'poly') {
      out.receiver_clock = {
        model: 'poly',
        bias_s: num('rxclk-bias'),
        drift_s_per_s: num('rxclk-drift'),
        drift_rate_s_per_s2: num('rxclk-driftrate'),
        sawtooth_amp_s: num('rxclk-sawamp'),
        sawtooth_period_s: num('rxclk-sawper'),
      };
    }
    if (val('mp') === 'specular') {
      const refs = [];
      for (const k of ['mp1', 'mp2']) {
        const amp = num(`${k}-amp`);
        if (amp > 0) refs.push({
          excess_delay_m: num(`${k}-delay`), amplitude: amp, phase_rad: num(`${k}-phase`),
        });
      }
      if (refs.length) out.multipath = { model: 'specular', reflections: refs };
    }
    if (Object.keys(out).length === 0) return null;
    out.models_to_iq = document.getElementById(`${id}-mdl-to-iq`).checked;
    return out;
  }

  // Fold the opt-in "Signal engine" panel into the /api/generate body.
  // Every control sits at its default (engine gps-sdr-sim, quant int16,
  // fading off, blank fs) -> returns null -> the request body is unchanged.
  function _engineBody() {
    const eng = document.getElementById(`${id}-engine`).value;
    const fs = document.getElementById(`${id}-fs`).value;
    const quant = document.getElementById(`${id}-quant`).value;
    const fm = document.getElementById(`${id}-fade-model`).value;
    const out = {};
    if (eng && eng !== 'gps-sdr-sim') out.engine = eng;
    if (fs) out.sample_rate = Number(fs);
    if (quant && quant !== 'int16') out.sample_format = quant;
    if (fm && fm !== 'off') {
      out.fading = {
        model: fm,
        sigma_db: Number(document.getElementById(`${id}-fade-sigma`).value),
        coherence_s: Number(document.getElementById(`${id}-fade-coh`).value),
        seed: Number(document.getElementById(`${id}-fade-seed`).value),
      };
    }
    const sys = ["G", "R", "E", "C", "J", "S"].filter(
      s => document.getElementById(`${id}-sys-${s}`).checked);
    if (sys.length > 1) out.systems = sys;
    return Object.keys(out).length ? out : null;
  }

  function _renderModelSummary(cm) {
    const el = document.getElementById(`${id}-mdl-summary`);
    if (!cm || !cm.any_enabled) { el.textContent = ''; return; }
    const bits = [];
    if (cm.ionosphere_model && cm.ionosphere_model !== 'off')
      bits.push(`iono ${cm.ionosphere_model} ≈ ${(cm.ionosphere_delay_m || 0).toFixed(2)} m @ ${cm.atmosphere_sample_el_deg}° el`);
    if (cm.troposphere_model && cm.troposphere_model !== 'off')
      bits.push(`tropo ${cm.troposphere_model} ≈ ${(cm.troposphere_delay_m || 0).toFixed(2)} m (truth only)`);
    if (cm.receiver_clock_model && cm.receiver_clock_model !== 'off')
      bits.push(`rx clock ${(cm.receiver_clock_offset_s * 1e6).toFixed(3)} µs = ${cm.receiver_clock_range_bias_m.toFixed(1)} m, ${cm.receiver_clock_carrier_offset_hz.toFixed(2)} Hz`);
    if (cm.multipath_model && cm.multipath_model !== 'off')
      bits.push(`multipath ${cm.multipath_n_reflections} refl → code ${cm.multipath_code_bias_m.toFixed(2)} m, carrier ${cm.multipath_carrier_bias_m.toFixed(3)} m`);
    el.textContent = 'applied: ' + bits.join('  ·  ');
  }

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
      ephemeris_mode: document.getElementById(`${id}-eph-mode`).value,
    };
    if (st.route) body.route = st.route;
    const _imp = _impairmentsBody();
    if (_imp) { body.impairments = _imp; body.random_seed = _imp.seed; }
    const _mdl = _channelModelsBody();
    if (_mdl) Object.assign(body, _mdl);
    const _eng = _engineBody();
    Object.assign(body, _eng || {});
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
              drawSvPowerTable(`${id}-svpower-table`, msg.done.svs, msg.done.bands);
              const corrLabel = document.getElementById(`${id}-iq-correlation-label`);
              if (corrLabel) {
                corrLabel.textContent = (msg.done.inspect && msg.done.inspect.length)
                  ? 'Acquisition metric (dB)'
                  : 'Per-SV signal power (gain dB, all constellations)';
              }
              drawCorrelationBars(`${id}-iq-correlation`, msg.done.inspect, msg.done.svs);
              loadIqPlots(id, msg.done.outdir);
              attachIqScrubber(id, msg.done.outdir);
              if (st.lastSatellites) {
                const cn0 = {};
                (msg.done.inspect || []).forEach(r => { cn0[r.prn] = r.metric_db; });
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
    if (document.getElementById(`${id}-record`).checked) body.record = true;
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
          if (msg.finished) {
            badge.textContent = 'STOPPED'; badge.classList.remove('badge-live'); disableLiveTabs(id);
            if (document.getElementById(`${id}-record`).checked) _refreshReplayOptions();
          }
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
