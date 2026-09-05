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
    const detail = Object.entries(rest).map(([k, v]) => `${k}=${v}`).join(' ');
    li.textContent = `[${t}] ${event}${detail ? ' ' + detail : ''}`;
    list.appendChild(li);
  });
};
