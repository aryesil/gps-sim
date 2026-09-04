// frontend/pages.js
window.showPage = function (name) {
  document.querySelectorAll('#page-container > section').forEach(sec => {
    sec.hidden = sec.dataset.page !== name;
  });
  document.querySelectorAll('.side-icon').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.page === name);
  });
};

window.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.side-icon').forEach(btn => {
    btn.onclick = () => showPage(btn.dataset.page);
  });
});
