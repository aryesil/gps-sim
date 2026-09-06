from fastapi.testclient import TestClient
from backend.app import app

client = TestClient(app)
_BASE = {"lat": 41.0, "lon": 29.0, "alt": 100.0,
         "start_utc": "2026-09-01T00:00:00", "duration_s": 1}


def test_unknown_system_letter_422():
    r = client.post("/api/generate", json={**_BASE, "engine": "native",
                                           "systems": ["G", "X"]})
    assert r.status_code == 422 and "systems" in r.json()["detail"]


def test_gps_sdr_sim_rejects_non_gps_systems_422():
    r = client.post("/api/generate", json={**_BASE, "engine": "gps-sdr-sim",
                                           "systems": ["G", "E"]})
    assert r.status_code == 422


def test_absent_systems_is_gps_only_not_422():
    r = client.post("/api/generate", json={**_BASE, "engine": "native"})
    assert r.status_code != 422
