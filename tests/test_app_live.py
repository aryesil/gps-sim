# tests/test_app_live.py
import json

import numpy as np
from fastapi.testclient import TestClient

from backend import config
from backend.app import _tee_spectrogram, app

client = TestClient(app)


def test_tee_spectrogram_forwards_chunks_unchanged_and_emits_one_row_per_chunk():
    chunks = [np.ones(512, dtype=np.complex64) * (k + 1) for k in range(3)]
    rows = []
    out = list(_tee_spectrogram(iter(chunks), sample_rate=2_600_000.0,
                                 on_row=lambda freqs, db: rows.append((freqs, db))))
    assert len(out) == 3
    for orig, forwarded in zip(chunks, out):
        assert np.array_equal(orig, forwarded)
    assert len(rows) == 3
    freqs, db = rows[0]
    assert len(freqs) == len(db) == 256  # nfft passed to inspector.spectrum
    assert np.all(np.isfinite(db))


def test_live_start_needs_allow_tx_and_confirm(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", False)
    r = client.post("/api/live/start", json={
        "rinex_path": "AUTO", "lat": 0, "lon": 0, "alt": 0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True})
    assert r.status_code == 403


def test_third_channel_rejected_when_both_slots_full(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    from backend import app as app_module
    app_module._tx_slots["TX1"] = {"stop": __import__("threading").Event(), "session": None}
    app_module._tx_slots["TX2"] = {"stop": __import__("threading").Event(), "session": None}
    try:
        r = client.post("/api/live/start", json={
            "rinex_path": "AUTO", "lat": 0, "lon": 0, "alt": 0,
            "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True})
        assert r.status_code == 409
    finally:
        app_module._tx_slots["TX1"] = None
        app_module._tx_slots["TX2"] = None


def test_jog_unknown_slot_404():
    r = client.post("/api/live/jog", json={"slot": "TX1", "direction": "north", "distance_m": 10})
    assert r.status_code == 404


def test_stop_transmit_no_slots_occupied_is_noop():
    from backend import app as app_module
    app_module._tx_slots["TX1"] = None
    app_module._tx_slots["TX2"] = None
    r = client.post("/api/transmit/stop", json={})
    assert r.status_code == 200
    assert r.json() == {"stopped": True}


def test_stop_transmit_one_slot_occupied_autopicks_it():
    from backend import app as app_module
    import threading
    ev = threading.Event()
    app_module._tx_slots["TX1"] = {"stop": ev, "session": None}
    app_module._tx_slots["TX2"] = None
    try:
        r = client.post("/api/transmit/stop", json={})
        assert r.status_code == 200
        assert ev.is_set()
    finally:
        app_module._tx_slots["TX1"] = None
        app_module._tx_slots["TX2"] = None


def test_stop_transmit_both_occupied_without_slot_400():
    from backend import app as app_module
    import threading
    app_module._tx_slots["TX1"] = {"stop": threading.Event(), "session": None}
    app_module._tx_slots["TX2"] = {"stop": threading.Event(), "session": None}
    try:
        r = client.post("/api/transmit/stop", json={})
        assert r.status_code == 400
    finally:
        app_module._tx_slots["TX1"] = None
        app_module._tx_slots["TX2"] = None


class _FakeSession:
    def __init__(self):
        self.stopped = False

        class _State:
            llh = (0.0, 0.0, 0.0)
            time_offset_s = 0.0

        self.state = _State()

    def jog(self, direction, distance_m):
        pass

    def shift_time(self, field, delta):
        setattr(self.state, field, getattr(self.state, field) + delta)

    def stop(self):
        self.stopped = True


def test_live_stop_sets_event_and_stops_session():
    from backend import app as app_module
    import threading
    session = _FakeSession()
    ev = threading.Event()
    app_module._tx_slots["TX1"] = {"stop": ev, "session": session}
    try:
        r = client.post("/api/live/stop", json={"slot": "TX1"})
        assert r.status_code == 200
        assert ev.is_set()
        assert session.stopped is True
    finally:
        app_module._tx_slots["TX1"] = None


def test_live_time_shift_updates_field():
    from backend import app as app_module
    import threading
    session = _FakeSession()
    app_module._tx_slots["TX1"] = {"stop": threading.Event(), "session": session}
    try:
        r = client.post("/api/live/time_shift",
                         json={"slot": "TX1", "field": "time_offset_s", "delta": 5.0})
        assert r.status_code == 200
        body = r.json()
        assert body["time_offset_s"] == 5.0
    finally:
        app_module._tx_slots["TX1"] = None


def test_transmit_409_when_both_slots_full(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    from backend import app as app_module
    app_module._tx_slots["TX1"] = {"stop": __import__("threading").Event(), "session": None}
    app_module._tx_slots["TX2"] = {"stop": __import__("threading").Event(), "session": None}
    iq = tmp_path / "gpssim.bin"
    iq.write_bytes(b"\x00\x00" * 100)
    try:
        r = client.post("/api/transmit", json={
            "iq_path": str(iq), "sample_rate": 2.6e6, "sample_format": "int16",
            "confirm_isolated": True})
        assert r.status_code == 409
    finally:
        app_module._tx_slots["TX1"] = None
        app_module._tx_slots["TX2"] = None
