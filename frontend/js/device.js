// frontend/device.js
// SDR "Connect" = open a standby control link (backend/device.py). The
// radio emits nothing until an actual transmit starts; this just proves
// the link and shows hardware identity / die temperature.
window.deviceUI = (function () {
  function fmtInfo(info) {
    if (!info) return '';
    const bits = [];
    if (info.hw_model) bits.push(info.hw_model);
    if (info.hw_serial) bits.push(String(info.hw_serial).slice(0, 8));
    if (info.fw_version) bits.push(info.fw_version);
    if (info.temp_c != null) bits.push(info.temp_c + ' °C');
    return bits.join(' · ');
  }

  async function refreshGlobal() {
    let devs = [];
    try {
      const r = await fetch('/api/device/status');
      devs = (await r.json()).devices || [];
    } catch (e) { /* leave devs empty */ }
    const el = document.getElementById('device-indicator');
    if (el) {
      const on = devs.length > 0;
      el.classList.toggle('dev-on', on);
      el.classList.toggle('dev-off', !on);
      el.textContent = on
        ? `◉ SDR STANDBY · ${devs.length} link${devs.length > 1 ? 's' : ''}`
        : '◌ SDR OFFLINE';
    }
    return devs;
  }

  async function wireChannel(id) {
    const btn = document.getElementById(`${id}-dev-connect`);
    const st = document.getElementById(`${id}-dev-status`);
    if (!btn || !st) return;

    function setState(connected, info) {
      btn.textContent = connected ? 'Disconnect' : 'Connect';
      btn.dataset.connected = connected ? '1' : '';
      st.className = 'dev-status ' + (connected ? 'dev-on' : 'dev-off');
      const extra = connected && fmtInfo(info) ? ' · ' + fmtInfo(info) : '';
      st.textContent = connected ? `standby · connected${extra}` : 'not connected';
    }

    btn.onclick = async () => {
      const uri = document.getElementById(`${id}-uri`).value.trim();
      const connected = btn.dataset.connected === '1';
      btn.disabled = true;
      try {
        if (connected) {
          await fetch('/api/device/disconnect', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uri }),
          });
          setState(false);
          if (window.logLine) logLine(`Channel ${id}: SDR ${uri} disconnected`, 'info');
        } else {
          const r = await fetch('/api/device/connect', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uri }),
          });
          const d = await r.json();
          if (!r.ok) {
            alert('connect failed: ' + (d.detail || ('HTTP ' + r.status)));
          } else {
            setState(true, d.info);
            if (window.logLine) logLine(`Channel ${id}: SDR ${uri} connected (standby)`, 'info');
          }
        }
      } finally {
        btn.disabled = false;
        refreshGlobal();
      }
    };

    // Reflect any link the server already holds for this card's URI.
    const devs = await refreshGlobal();
    const uri = document.getElementById(`${id}-uri`).value.trim();
    const mine = devs.find((x) => x.uri === uri);
    setState(!!mine, mine && mine.info);
  }

  setInterval(refreshGlobal, 10000);
  return { wireChannel, refreshGlobal };
})();
