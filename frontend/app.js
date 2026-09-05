// frontend/app.js (trimmed)

// RBAC: if the operator has an API key configured (backend/auth.py,
// disabled by default), attach it as X-API-Key to every request this
// page makes, rather than threading the header through every fetch()
// call site individually.
(function () {
  let key = null;
  try { key = localStorage.getItem('gpssim_api_key'); } catch (e) { /* private mode etc */ }
  if (!key) return;
  const origFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    init = init || {};
    init.headers = new Headers(init.headers || {});
    init.headers.set('X-API-Key', key);
    return origFetch(input, init);
  };
})();

window.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('btn-api-key');
  if (!btn) return;
  btn.onclick = () => {
    let current = '';
    try { current = localStorage.getItem('gpssim_api_key') || ''; } catch (e) { /* ignore */ }
    const next = prompt('Operator/viewer API key (leave blank to clear; RBAC is a no-op if the backend has no keys configured):', current);
    if (next === null) return;
    try {
      if (next.trim()) localStorage.setItem('gpssim_api_key', next.trim());
      else localStorage.removeItem('gpssim_api_key');
    } catch (e) { /* ignore */ }
    location.reload();  // simplest way to re-apply the fetch() patch above
  };
});

window.openConfirmModal = function (onConfirm) {
  const modal = document.getElementById('confirm-modal');
  const input = document.getElementById('confirm-modal-input');
  const ok = document.getElementById('confirm-modal-ok');
  input.value = ''; ok.disabled = true; modal.hidden = false;
  input.oninput = () => { ok.disabled = input.value.trim() !== 'TRANSMIT'; };
  document.getElementById('confirm-modal-cancel').onclick = () => { modal.hidden = true; };
  ok.onclick = () => { modal.hidden = true; onConfirm(); };
  input.focus();
};

window.addEventListener('DOMContentLoaded', () => {
  addChannel();   // start with one channel card
  document.getElementById('btn-add-channel').onclick = () => addChannel();
  // "Start All": each card owns its own start logic (validation, body
  // construction, SSE pump), so fan out to those buttons rather than
  // duplicating it here.
  document.getElementById('btn-start-all').onclick = () => {
    document.querySelectorAll('#channel-list .channel-card').forEach(card => {
      const btn = document.getElementById(`${card.id}-start`);
      if (btn) btn.click();
    });
  };
});
