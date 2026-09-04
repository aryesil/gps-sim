// frontend/map.js
window.gpsMap = (function () {
  let marker = null, cb = null;
  function init() {
    const m = L.map('map').setView([41.0082, 28.9784], 6);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      { attribution: 'OSM' }).addTo(m);
    m.on('click', (e) => {
      if (marker) marker.remove();
      marker = L.marker(e.latlng).addTo(m);
      document.getElementById('rx-readout').textContent =
        `RX ${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)}`;
      if (cb) cb(e.latlng.lat, e.latlng.lng);
    });
  }
  return { init, onPick: (f) => { cb = f; }, latlng: () => marker && marker.getLatLng() };
})();
