// frontend/app.js (trimmed)
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
