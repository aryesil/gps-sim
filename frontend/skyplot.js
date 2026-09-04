// frontend/skyplot.js
window.drawSkyplot = function (entries) {
  const c = document.getElementById('skyplot'), g = c.getContext('2d');
  const cx = c.width / 2, cy = c.height / 2, R = Math.min(cx, cy) - 10;
  g.clearRect(0, 0, c.width, c.height);
  g.strokeStyle = '#ccc';
  [1, 2 / 3, 1 / 3].forEach(k => { g.beginPath(); g.arc(cx, cy, R * k, 0, 7); g.stroke(); });
  g.fillStyle = '#06c';
  entries.forEach(e => {
    const r = R * (1 - e.el_deg / 90), a = (e.az_deg - 90) * Math.PI / 180;
    const x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
    g.beginPath(); g.arc(x, y, 4, 0, 7); g.fill();
    g.fillText('G' + e.prn, x + 5, y);
  });
};
