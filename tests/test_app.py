# tests/test_app.py
import datetime as dt

from fastapi.testclient import TestClient

from backend import app as appmod

client = TestClient(appmod.app)


def test_health_shape():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"gps_sdr_sim", "georinex", "libiio", "allow_tx"}


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
