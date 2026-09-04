// frontend/live.js
window.enableLiveTabs = function (channelId, txSlot) {
  const id = channelId;
  channelState(id).txSlot = txSlot;
  document.getElementById(`${id}-live-hint`).hidden = true;
  document.getElementById(`${id}-jog-controls`).hidden = false;
  document.getElementById(`${id}-time-controls`).hidden = false;

  document.querySelectorAll(`#${id}-jog-controls button[data-dir]`).forEach(btn => {
    btn.onclick = async () => {
      const distance_m = Number(document.getElementById(`${id}-jog-step`).value);
      const r = await fetch('/api/live/jog', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slot: txSlot, direction: btn.dataset.dir, distance_m }),
      });
      const d = await r.json();
      if (r.ok) document.getElementById(`${id}-live-llh`).textContent = JSON.stringify(d.llh);
      else if (window.logLine) logLine('live jog failed: ' + JSON.stringify(d), 'error');
    };
  });

  document.querySelectorAll(`#${id}-time-controls button[data-field]`).forEach(btn => {
    btn.onclick = async () => {
      const r = await fetch('/api/live/time_shift', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slot: txSlot, field: btn.dataset.field, delta: Number(btn.dataset.delta) }),
      });
      const d = await r.json();
      if (!r.ok) { if (window.logLine) logLine('live time_shift failed: ' + JSON.stringify(d), 'error'); return; }
      document.getElementById(`${id}-time-offset`).value = d.time_offset_s;
    };
  });
};

window.disableLiveTabs = function (channelId) {
  document.getElementById(`${channelId}-live-hint`).hidden = false;
  document.getElementById(`${channelId}-jog-controls`).hidden = true;
  document.getElementById(`${channelId}-time-controls`).hidden = true;
  channelState(channelId).txSlot = null;
};
