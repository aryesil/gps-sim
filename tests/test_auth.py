# tests/test_auth.py
from fastapi.testclient import TestClient

from backend import config
from backend.app import app

client = TestClient(app)


def test_no_keys_configured_means_auth_disabled(monkeypatch):
    monkeypatch.setattr(config, "API_KEYS", {})
    monkeypatch.setattr(config, "ALLOW_TX", False)
    # /api/live/start with no X-API-Key still reaches the ALLOW_TX check
    # (403 for that reason, not for missing auth) -- proves RBAC is a no-op.
    r = client.post("/api/live/start", json={
        "rinex_path": "AUTO", "lat": 0, "lon": 0, "alt": 0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True})
    assert r.status_code == 403
    assert "ALLOW_TX" in r.json()["detail"]


def test_operator_endpoint_rejects_missing_key_when_configured(monkeypatch):
    monkeypatch.setattr(config, "API_KEYS", {"op-key": "operator", "view-key": "viewer"})
    monkeypatch.setattr(config, "ALLOW_TX", True)
    r = client.post("/api/live/start", json={
        "rinex_path": "AUTO", "lat": 0, "lon": 0, "alt": 0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True})
    assert r.status_code == 403
    assert "operator role required" in r.json()["detail"]


def test_operator_endpoint_rejects_viewer_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEYS", {"op-key": "operator", "view-key": "viewer"})
    monkeypatch.setattr(config, "ALLOW_TX", True)
    r = client.post("/api/live/start", headers={"X-API-Key": "view-key"}, json={
        "rinex_path": "AUTO", "lat": 0, "lon": 0, "alt": 0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True})
    assert r.status_code == 403
    assert "operator role required" in r.json()["detail"]


def test_operator_endpoint_accepts_operator_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEYS", {"op-key": "operator"})
    monkeypatch.setattr(config, "ALLOW_TX", False)
    r = client.post("/api/live/start", headers={"X-API-Key": "op-key"}, json={
        "rinex_path": "AUTO", "lat": 0, "lon": 0, "alt": 0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True})
    # Auth passed -- now falls through to the pre-existing ALLOW_TX check.
    assert r.status_code == 403
    assert "ALLOW_TX" in r.json()["detail"]


def test_stop_endpoint_allows_viewer_key(monkeypatch):
    """Stopping is safety-positive: both roles may issue it."""
    monkeypatch.setattr(config, "API_KEYS", {"view-key": "viewer"})
    r = client.post("/api/transmit/stop", headers={"X-API-Key": "view-key"}, json={})
    assert r.status_code == 200


def test_stop_endpoint_rejects_unknown_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEYS", {"view-key": "viewer"})
    r = client.post("/api/transmit/stop", headers={"X-API-Key": "nope"}, json={})
    assert r.status_code == 403
