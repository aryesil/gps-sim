# tests/test_receiver_feed.py
import pytest
from fastapi.testclient import TestClient

from backend import receiver_feed
from backend.app import app

client = TestClient(app)

_GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"


@pytest.fixture(autouse=True)
def _reset_listener():
    receiver_feed.stop_listen()
    yield
    receiver_feed.stop_listen()


def test_inject_parses_and_stores_latest_fix():
    parsed = receiver_feed.inject(_GGA)
    assert parsed["sentence"] == "GGA"
    assert receiver_feed.latest_fix()["sentence"] == "GGA"


def test_inject_malformed_sentence_returns_none_and_does_not_overwrite():
    receiver_feed.inject(_GGA)
    result = receiver_feed.inject("garbage")
    assert result is None
    assert receiver_feed.latest_fix()["sentence"] == "GGA"  # unchanged


def test_start_listen_udp_then_stop(tmp_path):
    receiver_feed.start_listen("udp", host="127.0.0.1", port=0)
    assert receiver_feed.is_listening()
    receiver_feed.stop_listen()
    assert not receiver_feed.is_listening()


def test_start_listen_unknown_mode_raises():
    with pytest.raises(ValueError):
        receiver_feed.start_listen("carrier-pigeon")


def test_api_inject_and_read_fix():
    r = client.post("/api/receiver/inject", json={"sentence": _GGA})
    assert r.status_code == 200
    assert r.json()["fix"]["sentence"] == "GGA"

    r = client.get("/api/receiver/fix")
    assert r.json()["fix"]["sentence"] == "GGA"


def test_api_inject_rejects_malformed():
    r = client.post("/api/receiver/inject", json={"sentence": "garbage"})
    assert r.status_code == 400


def test_api_listen_udp_then_stop():
    r = client.post("/api/receiver/listen", json={"mode": "udp", "host": "127.0.0.1", "port": 0})
    assert r.status_code == 200
    assert client.get("/api/receiver/fix").json()["listening"] is True
    r = client.post("/api/receiver/stop_listen")
    assert r.status_code == 200
    assert client.get("/api/receiver/fix").json()["listening"] is False


def test_api_listen_unknown_mode_400():
    r = client.post("/api/receiver/listen", json={"mode": "carrier-pigeon"})
    assert r.status_code == 400
