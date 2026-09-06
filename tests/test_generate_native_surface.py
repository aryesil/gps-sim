from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_generate_rejects_unknown_engine():
    r = client.post("/api/generate", json={
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "duration_s": 1, "engine": "bogus"})
    assert r.status_code == 422


def test_generate_rejects_bad_fading():
    r = client.post("/api/generate", json={
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "duration_s": 1,
        "engine": "native", "fading": {"model": "lognormal", "coherence_s": 0}})
    assert r.status_code == 422


def test_generate_rejects_int12_on_gps_sdr_sim_path():
    for extra in ({}, {"engine": "gps-sdr-sim"}):
        r = client.post("/api/generate", json={
            "lat": 41.0, "lon": 29.0, "alt": 100.0,
            "start_utc": "2024-01-01T00:00:00", "duration_s": 1,
            "sample_format": "int12", **extra})
        assert r.status_code == 422
        assert "int12" in r.json()["detail"]


def test_generate_allows_int12_with_native_engine():
    # int12 + engine=native must not be rejected by the format/engine check.
    # It may still fail later (e.g. 503 for a missing RINEX in this env); we only
    # assert it is not *this* 422.
    r = client.post("/api/generate", json={
        "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "duration_s": 1,
        "engine": "native", "sample_format": "int12"})
    assert r.status_code != 422 or "int12" not in r.json().get("detail", "")
