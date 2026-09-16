# tests/test_app_live.py
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.app import _tee_spectrogram, app
from backend.rf import transmit

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


def test_tee_spectrogram_tracks_prn_when_requested():
    rng = np.random.default_rng(0)
    chunks = [(rng.standard_normal(4096) + 1j * rng.standard_normal(4096)).astype(np.complex64)
              for _ in range(2)]
    cn0_samples = []
    out = list(_tee_spectrogram(iter(chunks), sample_rate=2_600_000.0,
                                 on_row=lambda f, d: None, track_prn=1,
                                 on_cn0=lambda db: cn0_samples.append(db)))
    assert len(out) == 2
    assert len(cn0_samples) == 2
    assert all(isinstance(v, float) for v in cn0_samples)


def test_tee_spectrogram_skips_cn0_without_track_prn():
    chunks = [np.ones(512, dtype=np.complex64)]
    cn0_samples = []
    list(_tee_spectrogram(iter(chunks), sample_rate=2_600_000.0,
                           on_row=lambda f, d: None,
                           on_cn0=lambda db: cn0_samples.append(db)))
    assert cn0_samples == []


def test_internal_bands_for_no_bands_uses_each_systems_own_default():
    from backend.app import _internal_bands_for
    assert _internal_bands_for(["G"], None) == {"L1"}
    # GPS -> L1, GLONASS -> its own G1: two outputs with no "bands" sent.
    assert _internal_bands_for(["G", "R"], None) == {"L1", "G1"}


def test_internal_bands_for_explicit_bands_drops_systems_with_no_signal_there():
    from backend.app import _internal_bands_for
    # GLONASS has no signal on "L1" (only G1) -- explicit bands=["L1"]
    # silently contributes nothing for it, unlike the no-bands-sent case.
    assert _internal_bands_for(["G", "R"], ["L1"]) == {"L1"}
    assert _internal_bands_for(["G"], ["L2"]) == {"L2"}


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


def test_requested_tx_slot_normalizes_auto_and_junk_to_none():
    from backend.app import _requested_tx_slot
    assert _requested_tx_slot({}) is None
    assert _requested_tx_slot({"slot": "auto"}) is None
    assert _requested_tx_slot({"slot": ""}) is None
    assert _requested_tx_slot({"slot": "TX3"}) is None
    assert _requested_tx_slot({"slot": "TX1"}) == "TX1"
    assert _requested_tx_slot({"slot": "TX2"}) == "TX2"


def test_acquire_tx_slot_explicit_pick_honoured_and_conflict_rejected():
    from backend.app import _acquire_tx_slot, _release_tx_slot
    from fastapi import HTTPException
    slot = _acquire_tx_slot("TX2")
    try:
        assert slot == "TX2"
        with pytest.raises(HTTPException) as exc:
            _acquire_tx_slot("TX2")
        assert exc.value.status_code == 409
        # TX1 is still free -- an explicit TX2 request must never silently
        # fall back to it.
        other = _acquire_tx_slot("TX1")
        _release_tx_slot(other)
    finally:
        _release_tx_slot(slot)


def test_acquire_tx_slot_rejects_unknown_name():
    from backend.app import _acquire_tx_slot
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _acquire_tx_slot("TX3")
    assert exc.value.status_code == 400


def test_live_start_honours_explicit_slot_selection(monkeypatch):
    """The UI's TX1/TX2 selector must actually place the session on the
    port the operator picked, not whatever _acquire_tx_slot() finds free
    first."""
    import pathlib
    monkeypatch.setattr(config, "ALLOW_TX", True)
    from backend import app as app_module
    fixture = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"
    r = client.post("/api/live/start", json={
        "rinex_path": str(fixture), "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True,
        "slot": "TX2", "dry_run": True, "duration_s": 3600, "max_duration_s": 0.05})
    assert r.status_code == 200
    assert '"slot": "TX2"' in r.text
    assert app_module._tx_slots["TX1"] is None
    assert app_module._tx_slots["TX2"] is None  # released once finished


def test_live_start_sse_carries_rf_plan_when_layer_enabled(monkeypatch):
    # start_transmit's SSE already merged rf_report into every event; live_start's
    # own cb() forgot to (only start_transmit did), leaving a client with no way
    # to observe the resolved tx_lo_hz/baseband_offset_hz on the live path.
    import pathlib
    monkeypatch.setattr(config, "ALLOW_TX", True)
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", True)
    fixture = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"
    r = client.post("/api/live/start", json={
        "rinex_path": str(fixture), "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True,
        "dry_run": True, "duration_s": 3600, "max_duration_s": 0.05,
        "target_rf_frequency_hz": 1_575_420_000, "lo_offset_mode": "MANUAL",
        "lo_offset_hz": -1_000_000})
    assert r.status_code == 200
    assert '"tx_lo_hz": 1574420000' in r.text


def test_live_start_explicit_slot_already_occupied_is_409_not_fallback(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    from backend import app as app_module
    app_module._tx_slots["TX1"] = {"stop": __import__("threading").Event(), "session": None}
    try:
        r = client.post("/api/live/start", json={
            "rinex_path": "AUTO", "lat": 0, "lon": 0, "alt": 0,
            "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True,
            "slot": "TX1", "dry_run": True})
        assert r.status_code == 409
        assert app_module._tx_slots["TX2"] is None  # never claimed as a fallback
    finally:
        app_module._tx_slots["TX1"] = None


def test_native_band_centre_defaults_to_gps_l1():
    r = client.get("/api/native/band_centre")
    assert r.status_code == 200
    assert r.json() == {"bands": {"L1": pytest.approx(config.L1_HZ)}}


def test_native_band_centre_resolves_per_system_default_when_bands_unset():
    r = client.get("/api/native/band_centre", params={"systems": "G,R", "bands": ""})
    assert r.status_code == 200
    body = r.json()["bands"]
    assert set(body) == {"L1", "G1"}
    assert body["L1"] == pytest.approx(config.L1_HZ)


def test_native_band_centre_resolves_explicit_band_request():
    r = client.get("/api/native/band_centre", params={"systems": "G", "bands": "L5"})
    assert r.status_code == 200
    assert r.json()["bands"] == {"L5": pytest.approx(config.L5_HZ)}


def test_native_band_centre_ignores_unknown_entries_instead_of_422ing():
    r = client.get("/api/native/band_centre", params={"systems": "G,ZZ", "bands": "L1,bogus"})
    assert r.status_code == 200
    assert r.json() == {"bands": {"L1": pytest.approx(config.L1_HZ)}}


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


def test_live_start_rejects_systems_bands_needing_two_outputs(monkeypatch):
    """GPS + GLONASS with no explicit `bands` (the UI's implicit-default
    request, sent whenever only L1 stays checked) resolves each system to
    its OWN native band -- GPS -> L1, GLONASS -> its own G1 -- so this is
    already two physical outputs with nothing "L2/L5" involved at all.
    Live transmit has exactly one output, so this must 422 before a TX
    slot is even acquired, and no slot should be left occupied."""
    monkeypatch.setattr(config, "ALLOW_TX", True)
    from backend import app as app_module
    r = client.post("/api/live/start", json={
        "rinex_path": "AUTO", "lat": 0, "lon": 0, "alt": 0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True,
        "engine": "native", "systems": ["G", "R"], "dry_run": True})
    assert r.status_code == 422
    assert "exactly one RF output" in r.json()["detail"]
    assert app_module._tx_slots["TX1"] is None
    assert app_module._tx_slots["TX2"] is None


def test_live_start_forwards_native_band_selection(monkeypatch):
    """The reported bug: bands/systems picked in the UI never reached
    /api/live/start at all -- Start always transmitted L1 regardless. A
    native engine=native, bands=["L5"] request must actually flow through
    to ScenarioRequest (proven by the SSE stream finishing without error
    against a GPS-only fixture, which has no signal on L1 disabled -- L5
    is a real distinct band with its own, much higher, sample-rate floor)."""
    import pathlib
    monkeypatch.setattr(config, "ALLOW_TX", True)
    fixture = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"
    r = client.post("/api/live/start", json={
        "rinex_path": str(fixture), "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True,
        "engine": "native", "systems": ["G"], "bands": ["L5"],
        "dry_run": True, "duration_s": 3600, "max_duration_s": 0.05})
    assert r.status_code == 200
    body = r.text
    assert '"error"' not in body
    assert '"finished": true' in body


def test_live_start_uses_the_resolved_bands_real_sample_rate(monkeypatch):
    """L5's native floor (>=25 Msps for GPS L5 I5/Q5) sits far above the
    request's nominal 2.6 Msps sample_rate, which only ever governs L1 --
    the SDR must be told L5's real rate, not the nominal one, or real
    hardware would play the IQ back at the wrong speed."""
    import pathlib
    monkeypatch.setattr(config, "ALLOW_TX", True)
    seen = {}
    real_stream = transmit.stream
    def spy_stream(params, **kwargs):
        seen["sample_rate"] = params.sample_rate
        seen["lo_hz"] = params.lo_hz
        return real_stream(params, **kwargs)
    monkeypatch.setattr(transmit, "stream", spy_stream)
    fixture = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"
    r = client.post("/api/live/start", json={
        "rinex_path": str(fixture), "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True,
        "engine": "native", "systems": ["G"], "bands": ["L5"],
        "sample_rate": 2.6e6, "dry_run": True, "duration_s": 3600,
        "max_duration_s": 0.05})
    assert r.status_code == 200
    assert seen["sample_rate"] == pytest.approx(20_500_000.0)
    assert seen["lo_hz"] == pytest.approx(config.L5_HZ)


def test_live_start_auto_stops_after_max_duration(monkeypatch):
    """Fail-safe: max_duration_s must end the live SSE stream (and set the
    slot's stop event) even if nobody calls /api/live/stop."""
    import pathlib
    monkeypatch.setattr(config, "ALLOW_TX", True)
    fixture = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"
    r = client.post("/api/live/start", json={
        "rinex_path": str(fixture), "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True,
        "dry_run": True, "duration_s": 3600, "max_duration_s": 0.05})
    assert r.status_code == 200
    body = r.text
    assert '"finished": true' in body
    from backend import app as app_module
    assert app_module._tx_slots["TX1"] is None or app_module._tx_slots["TX2"] is None


def test_apply_timeline_step_jog_calls_session_jog():
    from backend.app import _apply_timeline_step
    calls = []
    session = _FakeSession()
    session.jog = lambda direction, distance_m: calls.append((direction, distance_m))
    _apply_timeline_step(session, {"at_s": 5, "action": "jog", "direction": "north", "distance_m": 10})
    assert calls == [("north", 10.0)]


def test_apply_timeline_step_time_shift_updates_state():
    from backend.app import _apply_timeline_step
    session = _FakeSession()
    _apply_timeline_step(session, {"at_s": 1, "action": "time_shift", "field": "time_offset_s", "delta": 5.0})
    assert session.state.time_offset_s == 5.0


def test_apply_timeline_step_rejects_unknown_action():
    from backend.app import _apply_timeline_step
    session = _FakeSession()
    with pytest.raises(ValueError):
        _apply_timeline_step(session, {"at_s": 1, "action": "warp_drive"})


def test_live_start_runs_timeline_steps_in_order(monkeypatch):
    """End-to-end: a timeline entry due almost immediately (at_s near 0)
    must fire during the SSE stream and be reported both over SSE
    (timeline_step) and to the persistent audit log."""
    import pathlib
    monkeypatch.setattr(config, "ALLOW_TX", True)
    fixture = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"
    r = client.post("/api/live/start", json={
        "rinex_path": str(fixture), "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True,
        "dry_run": True, "duration_s": 3600, "max_duration_s": 0.3,
        "timeline": [{"at_s": 0, "action": "time_shift", "field": "time_offset_s", "delta": 7.0}]})
    assert r.status_code == 200
    body = r.text
    assert '"timeline_step"' in body
    assert '"field": "time_offset_s"' in body
