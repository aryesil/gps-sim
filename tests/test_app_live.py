# tests/test_app_live.py
import json

from fastapi.testclient import TestClient

from backend import config
from backend.app import app

client = TestClient(app)


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
