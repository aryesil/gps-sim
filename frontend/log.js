// frontend/log.js
window.logLine = function (text, level) {
  const list = document.getElementById('log-list');
  const li = document.createElement('li');
  li.className = 'log-' + (level || 'info');
  li.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  list.insertBefore(li, list.firstChild);
  while (list.children.length > 200) list.removeChild(list.lastChild);
};
