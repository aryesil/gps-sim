// frontend/pages.js
window.showPage = function (name) {
  document.querySelectorAll('#page-container > section').forEach(sec => {
    sec.hidden = sec.dataset.page !== name;
  });
  document.querySelectorAll('.side-icon').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.page === name);
  });
  // Leaflet computes tile layout from its container's size at the moment
  // the map is created; a map created (or left) under display:none gets
  // a 0-size layout that never repairs itself once shown. Nudge it after
  // the page becomes visible.
  if (name === 'trajectory' && window._tbMap) {
    setTimeout(() => window._tbMap.invalidateSize(), 0);
  }
  if (name === 'channels' && window.gpsMap) {
    setTimeout(() => window.gpsMap.invalidateAll(), 0);
  }
};

window.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.side-icon').forEach(btn => {
    btn.onclick = () => showPage(btn.dataset.page);
  });
});
