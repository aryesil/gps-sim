// frontend/map.js
window.gpsMap = (function () {
  const instances = {};   // mapElementId -> {map, marker, cb}

  function init(mapElementId) {
    if (instances[mapElementId]) return instances[mapElementId].handle;
    const inst = { map: null, marker: null, cb: null };
    const m = L.map(mapElementId).setView([41.0082, 28.9784], 6);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      { attribution: 'OSM' }).addTo(m);
    inst.map = m;
    m.on('click', (e) => {
      if (inst.marker) inst.marker.remove();
      inst.marker = L.marker(e.latlng).addTo(m);
      const readout = document.getElementById(`${mapElementId}-readout`);
      if (readout) {
        readout.textContent = `RX ${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)}`;
      }
      if (inst.cb) inst.cb(e.latlng.lat, e.latlng.lng);
    });
    inst.handle = {
      onPick: (f) => { inst.cb = f; },
      latlng: () => inst.marker ? inst.marker.getLatLng() : null,
      invalidateSize: () => m.invalidateSize(),
      // Programmatic placement (e.g. loading a saved scenario) -- same
      // marker-replace + pan the click handler above does, just without
      // a click event driving it.
      setLatlng: (lat, lon) => {
        if (inst.marker) inst.marker.remove();
        inst.marker = L.marker([lat, lon]).addTo(m);
        m.panTo([lat, lon]);
        const readout = document.getElementById(`${mapElementId}-readout`);
        if (readout) readout.textContent = `RX ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
        if (inst.cb) inst.cb(lat, lon);
      },
    };
    instances[mapElementId] = inst;
    return inst.handle;
  }

  function invalidateAll() {
    Object.values(instances).forEach(inst => inst.map.invalidateSize());
  }

  return { init, invalidateAll };
})();
