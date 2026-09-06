// frontend/log.js
window.logLine = function (text, level) {
  const list = document.getElementById('log-list');
  const li = document.createElement('li');
  li.className = 'log-' + (level || 'info');
  li.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  list.insertBefore(li, list.firstChild);
  while (list.children.length > 200) list.removeChild(list.lastChild);
};

// Persistent audit trail: backend/audit.py records every transmit
// start/stop (manual or fail-safe auto-stop) to disk, so it survives a
// closed tab or a server restart. This merges that server-side history
// into the same #log-list the live logLine() calls populate.
// Real receiver feedback: closed-loop check against backend/receiver_feed.py
// -- start a UDP/serial NMEA listener and poll its latest parsed fix.
let _rfPollTimer = null;

async function _pollReceiverFix() {
  const r = await fetch('/api/receiver/fix');
  if (!r.ok) return;
  const d = await r.json();
  const out = document.getElementById('rf-fix-readout');
  if (!out) return;
  if (!d.listening) { out.textContent = 'not listening'; return; }
  if (!d.fix) { out.textContent = 'listening… no fix yet'; return; }
  const f = d.fix;
  out.textContent = f.sentence === 'GGA'
    ? `GGA fix: lat ${f.lat.toFixed(5)} lon ${f.lon.toFixed(5)} alt ${f.alt_m}m sats ${f.num_sats} hdop ${f.hdop}`
    : `RMC fix: lat ${f.lat.toFixed(5)} lon ${f.lon.toFixed(5)} ${f.status} speed ${f.speed_knots}kt`;
}

window.addEventListener('DOMContentLoaded', () => {
  const startBtn = document.getElementById('rf-listen-start');
  const stopBtn = document.getElementById('rf-listen-stop');
  if (!startBtn) return;  // panel not present on this page build
  startBtn.onclick = async () => {
    const mode = document.getElementById('rf-listen-mode').value;
    const addr = document.getElementById('rf-listen-addr').value.trim();
    const body = { mode };
    if (mode === 'udp') {
      const [host, port] = addr.split(':');
      body.host = host || '0.0.0.0';
      body.port = Number(port);
    } else {
      const [device, baud] = addr.split(':');
      body.device = device;
      if (baud) body.baud = Number(baud);
    }
    const r = await fetch('/api/receiver/listen', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!r.ok) { const d = await r.json(); return alert('listen failed: ' + (d.detail || '')); }
    logLine(`Receiver feed: listening (${mode}) on ${addr}`, 'info');
    if (_rfPollTimer) clearInterval(_rfPollTimer);
    _rfPollTimer = setInterval(_pollReceiverFix, 1000);
  };
  stopBtn.onclick = async () => {
    await fetch('/api/receiver/stop_listen', { method: 'POST' });
    if (_rfPollTimer) { clearInterval(_rfPollTimer); _rfPollTimer = null; }
    logLine('Receiver feed: stopped listening', 'info');
    _pollReceiverFix();
  };
});

// Multi-operator: a shared live feed of every audit event (transmit
// start/stop, timeline steps, auto-stop, ...) over backend/ws_hub.py's
// /ws/events, so every operator's open tab sees the same activity in
// real time -- not just the one that issued it.
window.connectEventsSocket = function () {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let ws;
  try {
    ws = new WebSocket(`${proto}//${location.host}/ws/events`);
  } catch (e) {
    return;  // e.g. no WebSocket support -- multi-operator live feed just doesn't run
  }
  ws.onmessage = (ev) => {
    let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
    const list = document.getElementById('log-list');
    if (!list) return;
    const li = document.createElement('li');
    li.className = 'log-audit';
    const detail = Object.entries(d).filter(([k]) => k !== 'ts' && k !== 'event')
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ');
    li.textContent = `[${new Date(d.ts).toLocaleTimeString()}] ${d.event}${detail ? ' ' + detail : ''}`;
    list.insertBefore(li, list.firstChild);
  };
  ws.onclose = () => { setTimeout(window.connectEventsSocket, 3000); };  // reconnect on drop
};

window.loadAuditLog = async function () {
  const r = await fetch('/api/audit?limit=200');
  if (!r.ok) return;
  const d = await r.json();
  const list = document.getElementById('log-list');
  d.events.forEach((ev) => {
    const li = document.createElement('li');
    li.className = 'log-audit';
    const t = new Date(ev.ts).toLocaleTimeString();
    const { ts, event, ...rest } = ev;
    const detail = Object.entries(rest)
      .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' ');
    li.textContent = `[${t}] ${event}${detail ? ' ' + detail : ''}`;
    list.appendChild(li);
  });
};
