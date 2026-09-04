// frontend/trajectory.js
let _tbMap = null, _tbMarkers = [], _tbWaypoints = [];
let _tbLine = null;

function _tbRedrawTable() {
  const t = document.getElementById('trajectory-table');
  t.innerHTML = '<tr><th>#</th><th>lat</th><th>lon</th><th>alt</th><th>speed m/s</th><th>accel m/s^2</th></tr>' +
    _tbWaypoints.map((w, i) => `<tr>
      <td>${i}</td>
      <td><input data-i="${i}" data-f="lat" value="${w.lat.toFixed(6)}" size="10"></td>
      <td><input data-i="${i}" data-f="lon" value="${w.lon.toFixed(6)}" size="10"></td>
      <td><input data-i="${i}" data-f="alt" value="${w.alt.toFixed(1)}" size="6"></td>
      <td><input data-i="${i}" data-f="speed" value="${w.speed}" size="5"></td>
      <td><input data-i="${i}" data-f="accel" value="${w.accel}" size="5"></td>
    </tr>`).join('');
  t.querySelectorAll('input').forEach(inp => {
    inp.onchange = () => {
      _tbWaypoints[Number(inp.dataset.i)][inp.dataset.f] = Number(inp.value);
      _tbRedrawMap();
    };
  });
  document.getElementById('tb-apply').disabled = _tbWaypoints.length < 2;
}

function _tbRedrawMap() {
  _tbMarkers.forEach(m => _tbMap.removeLayer(m));
  _tbMarkers = [];
  const latlngs = _tbWaypoints.map(w => [w.lat, w.lon]);
  _tbWaypoints.forEach((w, i) => {
    const m = L.marker([w.lat, w.lon], { draggable: true }).addTo(_tbMap)
      .bindTooltip(String(i), { permanent: true });
    m.on('dragend', () => {
      const ll = m.getLatLng();
      _tbWaypoints[i].lat = ll.lat; _tbWaypoints[i].lon = ll.lng;
      _tbRedrawTable(); _tbRedrawMap();
    });
    _tbMarkers.push(m);
  });
  if (_tbLine) _tbMap.removeLayer(_tbLine);
  _tbLine = L.polyline(latlngs, { color: 'red' }).addTo(_tbMap);
}

window.addEventListener('DOMContentLoaded', () => {
  _tbMap = L.map('trajectory-map').setView([52.0, 19.0], 6);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(_tbMap);
  _tbMap.on('click', (ev) => {
    _tbWaypoints.push({ lat: ev.latlng.lat, lon: ev.latlng.lng, alt: 100, speed: 5.0, accel: 1.0 });
    _tbRedrawTable(); _tbRedrawMap();
  });

  document.getElementById('tb-new').onclick = () => {
    _tbWaypoints = []; _tbRedrawTable(); _tbRedrawMap();
  };

  document.getElementById('tb-save').onclick = async () => {
    const name = prompt('Save trajectory as:');
    if (!name) return;
    const r = await fetch('/api/trajectory/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, waypoints: _tbWaypoints }),
    });
    if (!r.ok) {
      const d = await r.json();
      if (window.logLine) logLine('trajectory save failed: ' + (d.detail || JSON.stringify(d)), 'error');
      alert('save failed: ' + (d.detail || JSON.stringify(d)));
    }
  };

  document.getElementById('tb-open').onclick = async () => {
    const r = await fetch('/api/trajectory/list');
    const d = await r.json();
    const sel = document.getElementById('tb-open-list');
    sel.innerHTML = d.names.map(n => `<option>${n}</option>`).join('');
  };

  document.getElementById('tb-open-list').onchange = async (ev) => {
    const name = ev.target.value;
    if (!name) return;
    const r = await fetch(`/api/trajectory/load?name=${encodeURIComponent(name)}`);
    const d = await r.json();
    _tbWaypoints = d.waypoints;
    _tbRedrawTable(); _tbRedrawMap();
  };

  document.getElementById('tb-apply').onclick = () => {
    const firstCardId = document.querySelector('.channel-card')?.id;
    if (!firstCardId) return alert('add a channel first');
    channelState(firstCardId).route = _tbWaypoints.map(w => [w.lat, w.lon, w.alt]);
    alert(`Applied ${_tbWaypoints.length} waypoints to ${firstCardId}`);
  };
});
