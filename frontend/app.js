// frontend/app.js (trimmed)
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
