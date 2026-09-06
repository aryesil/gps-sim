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
