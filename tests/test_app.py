# tests/test_app.py
import datetime as dt
import pathlib

import pytest
from fastapi.testclient import TestClient

from backend import app as appmod

client = TestClient(appmod.app)

_MIXED = str(pathlib.Path(__file__).parent / "fixtures" / "brdc_mixed.rnx")


@pytest.fixture(name="client")
def _client_fixture():
    """The module-level TestClient above, exposed as a fixture for tests
    that request it by parameter (e.g. the RF-frontend tests below) without
    shadowing the module-global `client` name other tests reference directly."""
    return client


def test_preview_multi_returns_non_gps_systems():
    r = client.post("/api/preview", json={
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2026-09-01T12:00:00",
        "rinex_path": _MIXED, "systems": ["G", "E", "C", "R"]})
    assert r.status_code == 200, r.text
    sats = r.json()["satellites"]
    seen = {s["sys"] for s in sats}
    assert seen - {"G"}, f"only GPS came back: {seen}"
    assert all("svid" in s for s in sats)


def test_health_shape():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"gps_sdr_sim", "georinex", "libiio", "allow_tx",
                         "rf_frontend_enabled"}


def test_preview_warns_when_few_satellites(monkeypatch):
    monkeypatch.setattr(appmod.ephemeris, "get_ephemeris", lambda *a, **k: {1: {"toe": 0.0}})
    monkeypatch.setattr(appmod.geometry, "constellation", lambda *a, **k: [
        {"prn": 1, "az_deg": 10, "el_deg": 40, "geo_range_m": 2.1e7,
         "pseudorange_m": 2.1e7, "code_phase_chips": 3.0,
         "carrier_doppler_hz": 100.0, "code_doppler_hz": 0.06, "_los": [0, 0, 1]}])
    monkeypatch.setattr(appmod.geometry, "dop", lambda *a, **k: {"pdop": float("inf"),
        "gdop": float("inf"), "hdop": 1, "vdop": 1, "tdop": 1})
    r = client.post("/api/preview", json={"lat": 41.0, "lon": 29.0, "alt": 100.0,
                                          "start_utc": "2026-09-03T06:00:00"})
    assert r.status_code == 200
    assert any("4" in w for w in r.json()["warnings"])


def test_transmit_forbidden_without_confirm(monkeypatch):
    monkeypatch.setattr(appmod.config, "ALLOW_TX", True)
    r = client.post("/api/transmit", json={"iq_path": "/x/g.bin", "sample_rate": 2.6e6,
                                           "sample_format": "int16", "confirm_isolated": False})
    assert r.status_code == 403


def test_generate_refuses_when_disk_too_small(monkeypatch):
    monkeypatch.setattr(appmod, "download_free_bytes", lambda p: 1000)
    r = client.post("/api/generate", json={
        "rinex_path": "/x/brdc.rnx", "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2026-09-03T06:00:00", "duration_s": 300,
        "sample_rate": 2.6e6, "sample_format": "int16"})
    assert r.status_code == 507


def test_generate_rejects_bad_impairments(monkeypatch):
    monkeypatch.setattr(appmod, "download_free_bytes", lambda p: 10 ** 12)
    monkeypatch.setattr(appmod, "_resolve_rinex", lambda body, start: "/x/brdc.rnx")
    r = client.post("/api/generate", json={
        "rinex_path": "/x/brdc.rnx", "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2026-09-03T06:00:00", "duration_s": 4,
        "sample_rate": 2.6e6, "sample_format": "int16",
        "impairments": {"enabled_flag": True, "snr_db": 5, "noise_power": 1.0}})
    assert r.status_code == 422
    assert "impairments" in r.json()["detail"]


def test_preview_applies_channel_models_and_reports_summary(monkeypatch):
    monkeypatch.setattr(appmod.ephemeris, "get_ephemeris",
                        lambda *a, **k: {1: {"toe": 0.0}})
    seen = {}

    def _fake_constellation(eph, rx, tow, mask=5.0, **kw):
        seen.update(kw)
        return [{"prn": 1, "az_deg": 10, "el_deg": 40, "geo_range_m": 2.1e7,
                 "pseudorange_m": 2.1e7, "code_phase_chips": 3.0,
                 "carrier_doppler_hz": 100.0, "code_doppler_hz": 0.06,
                 "_los": [0, 0, 1]}]

    monkeypatch.setattr(appmod.geometry, "constellation", _fake_constellation)
    monkeypatch.setattr(appmod.geometry, "dop", lambda *a, **k: {
        "pdop": 2.0, "gdop": 2, "hdop": 1, "vdop": 1, "tdop": 1})
    r = client.post("/api/preview", json={
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2026-09-03T06:00:00",
        "receiver_clock": {"model": "poly", "bias_s": 1e-6},
        "atmosphere": {"troposphere": "saastamoinen"}})
    assert r.status_code == 200
    body = r.json()
    assert body["channel_models"]["receiver_clock_model"] == "poly"
    assert body["channel_models"]["troposphere_model"] == "saastamoinen"
    assert seen["rx_clock_range_m"] != 0.0
    assert callable(seen["atmo_delay_fn"])
    assert any("channel models applied" in w for w in body["warnings"])


def test_preview_rejects_bad_channel_model(monkeypatch):
    monkeypatch.setattr(appmod.ephemeris, "get_ephemeris",
                        lambda *a, **k: {1: {"toe": 0.0}})
    r = client.post("/api/preview", json={
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2026-09-03T06:00:00",
        "multipath": {"model": "bogus"}})
    assert r.status_code == 422


def test_health_reports_rf_frontend_enabled_flag(client, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", False)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["rf_frontend_enabled"] is False


def test_transmit_ignores_rf_frontend_fields_when_layer_disabled(client, monkeypatch, tmp_path):
    from backend import config
    monkeypatch.setattr(config, "ALLOW_TX", True)
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", False)
    p = tmp_path / "g.bin"
    p.write_bytes(b"\x00\x00" * 2000)
    body = {
        "iq_path": str(p), "sample_rate": 2.6e6, "sample_format": "int16",
        "confirm_isolated": True, "dry_run": True,
        "target_rf_frequency_hz": 1_575_420_000, "lo_offset_mode": "MANUAL",
        "lo_offset_hz": -1_000_000,
    }
    r = client.post("/api/transmit", json=body)
    assert r.status_code == 200
    # Consume the SSE stream to completion so the slot is released for other tests.
    list(r.iter_lines())


def test_transmit_applies_rf_plan_when_layer_enabled(client, monkeypatch, tmp_path):
    from backend import config
    monkeypatch.setattr(config, "ALLOW_TX", True)
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", True)
    p = tmp_path / "g.bin"
    p.write_bytes(b"\x00\x00" * 2000)
    body = {
        "iq_path": str(p), "sample_rate": 2.6e6, "sample_format": "int16",
        "confirm_isolated": True, "dry_run": True,
        "target_rf_frequency_hz": 1_575_420_000, "lo_offset_mode": "MANUAL",
        "lo_offset_hz": -1_000_000,
    }
    r = client.post("/api/transmit", json=body)
    assert r.status_code == 200
    lines = list(r.iter_lines())
    assert any(b'"tx_lo_hz": 1574420000' in line if isinstance(line, bytes)
               else '"tx_lo_hz": 1574420000' in line for line in lines)


def test_transmit_rejects_invalid_rf_plan_with_400(client, monkeypatch, tmp_path):
    from backend import config
    monkeypatch.setattr(config, "ALLOW_TX", True)
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", True)
    p = tmp_path / "g.bin"
    p.write_bytes(b"\x00\x00" * 2000)
    body = {
        "iq_path": str(p), "sample_rate": 2.6e6, "sample_format": "int16",
        "confirm_isolated": True, "dry_run": True,
        "target_rf_frequency_hz": 1_575_420_000, "lo_offset_mode": "MANUAL",
        "lo_offset_hz": -1_400_000,  # exceeds Nyquist at 2.6 Msps
    }
    r = client.post("/api/transmit", json=body)
    assert r.status_code == 400


def test_transmit_diagnostic_cw_mode_skips_iq_path_and_streams_a_tone(client, monkeypatch):
    # diagnostic_cw streams an infinite chunk_source (backend.rf.frontend.
    # diagnostic.cw_chunk_source never raises StopIteration) -- on real
    # hardware it runs until an operator calls /api/transmit/stop, so this
    # test drives the SSE response from a background thread and issues that
    # same stop call itself, instead of draining the stream to a natural end
    # that (by design) never comes.
    import threading

    from backend import config
    monkeypatch.setattr(config, "ALLOW_TX", True)
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", True)
    body = {
        "mode": "diagnostic_cw", "sample_rate": 2.6e6, "sample_format": "int16",
        "confirm_isolated": True, "dry_run": True,
    }
    result = {}

    def _drive():
        r = client.post("/api/transmit", json=body)
        result["status_code"] = r.status_code
        result["lines"] = list(r.iter_lines())

    t = threading.Thread(target=_drive)
    t.start()
    t.join(timeout=2.0)  # let the stream start and emit at least one chunk
    client.post("/api/transmit/stop")
    t.join(timeout=10.0)
    assert not t.is_alive(), "diagnostic_cw stream did not stop after /api/transmit/stop"

    assert result["status_code"] == 200
    lines = result["lines"]
    assert any(b'"finished": true' in line if isinstance(line, bytes)
               else '"finished": true' in line for line in lines)


def test_diagnostic_cw_mode_inert_when_rf_frontend_layer_disabled(client, monkeypatch, tmp_path):
    # RF_FRONTEND_ENABLED defaults False; diagnostic_cw must not become a
    # reachable RF-emitting mode without the feature's master switch --
    # with the flag off, "mode": "diagnostic_cw" must be ignored entirely
    # and the request must fall through to ordinary file-backed transmit
    # (today's exact pre-feature behavior), never to the infinite CW
    # generator.
    from backend import config
    iq_file = tmp_path / "g.bin"
    iq_file.write_bytes(b"\x00\x01" * 100)
    monkeypatch.setattr(config, "ALLOW_TX", True)
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", False)
    body = {
        "mode": "diagnostic_cw", "iq_path": str(iq_file), "sample_rate": 2.6e6,
        "sample_format": "int16", "confirm_isolated": True, "dry_run": True,
    }
    r = client.post("/api/transmit", json=body)
    assert r.status_code == 200
    lines = list(r.iter_lines())
    assert any(b'"finished": true' in line if isinstance(line, bytes)
               else '"finished": true' in line for line in lines)


def test_calibrate_rejected_when_rf_frontend_layer_disabled(client, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", False)
    monkeypatch.setattr(config, "ALLOW_TX", True)
    r = client.post("/api/tx/calibrate", json={"confirm_isolated": True,
                                                "target_rf_frequency_hz": 1_575_420_000})
    assert r.status_code == 403


def test_calibrate_rejected_without_allow_tx_or_confirm(client, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", True)
    monkeypatch.setattr(config, "ALLOW_TX", False)
    r = client.post("/api/tx/calibrate", json={"confirm_isolated": True,
                                                "target_rf_frequency_hz": 1_575_420_000})
    assert r.status_code == 403


def test_calibrate_rejected_when_a_slot_is_active(client, monkeypatch):
    from backend import config, app as app_module
    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", True)
    monkeypatch.setattr(config, "ALLOW_TX", True)
    app_module._tx_slots["TX1"] = {"stop": None, "session": None}
    try:
        r = client.post("/api/tx/calibrate", json={"confirm_isolated": True,
                                                    "target_rf_frequency_hz": 1_575_420_000})
        assert r.status_code == 409
    finally:
        app_module._tx_slots["TX1"] = None


def test_calibrate_runs_and_reports_result(client, monkeypatch):
    import sys
    import types
    from backend import config

    monkeypatch.setattr(config, "RF_FRONTEND_ENABLED", True)
    monkeypatch.setattr(config, "ALLOW_TX", True)

    class _FakeAttr:
        def __init__(self, value):
            self.value = value

    class _SettlingCalibModeAttr:
        # Real AD9361 driver reverts calib_mode away from the trigger value
        # once the one-shot calibration completes (see calibration.py's own
        # docstring) -- calibration.calibrate() polls for that revert. This
        # fake settles on the read right after the triggering write, mirroring
        # tests/test_rf_calibration.py's `_settle_reader` helper for Task 2.
        def __init__(self):
            self._value = "auto"

        @property
        def value(self):
            v = self._value
            if v == "tx_quad":
                self._value = "auto"
            return v

        @value.setter
        def value(self, v):
            self._value = v

    class _FakeCtrl:
        def __init__(self):
            self.attrs = {"calib_mode_available": _FakeAttr("auto manual tx_quad"),
                          "calib_mode": _SettlingCalibModeAttr()}

        def find_channel(self, name):
            raise Exception("no temp sensor in this fake")

    class _FakeSDR:
        def __init__(self, uri=None):
            self._ctrl = _FakeCtrl()
            self.sample_rate = None
            self.tx_lo = None

        def tx_destroy_buffer(self):
            pass

    fake_module = types.ModuleType("adi")
    fake_module.ad9361 = _FakeSDR
    monkeypatch.setitem(sys.modules, "adi", fake_module)

    r = client.post("/api/tx/calibrate", json={
        "confirm_isolated": True, "target_rf_frequency_hz": 1_575_420_000,
        "lo_offset_mode": "MANUAL", "lo_offset_hz": -1_000_000,
        "sample_rate": 2_600_000})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["calibration_type"] == "TX_QUAD"
    assert body["tx_lo_hz"] == 1_574_420_000
    assert body["target_rf_hz"] == 1_575_420_000
    assert body["baseband_offset_hz"] == 1_000_000
