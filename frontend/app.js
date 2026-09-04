// frontend/app.js (trimmed)
window.addEventListener('DOMContentLoaded', () => {
  addChannel();   // start with one channel card
  document.getElementById('btn-add-channel').onclick = () => addChannel();
});
