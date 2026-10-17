# tests/test_ws_hub.py
import pytest
from fastapi.testclient import TestClient

from backend.obs import audit
from backend import config
from backend.app import app


@pytest.fixture(scope="module")
def client():
    # Enter TestClient as a context manager so the app lifespan runs and
    # ws_hub._loop is pinned once to a single, persistent portal loop.
    # Without this each websocket_connect() can spin its own blocking
    # portal on a different loop, and a cross-loop broadcast then hangs.
    with TestClient(app) as c:
        yield c


def test_ws_events_receives_broadcast_audit_event(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    with client.websocket_connect("/ws/events") as ws:
        audit.log_event("manual_stop", slot="TX1")
        msg = ws.receive_json()
        assert msg["event"] == "manual_stop"
        assert msg["slot"] == "TX1"


def test_ws_events_multiple_clients_all_receive(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    with client.websocket_connect("/ws/events") as ws1, \
         client.websocket_connect("/ws/events") as ws2:
        audit.log_event("live_start", slot="TX2")
        assert ws1.receive_json()["slot"] == "TX2"
        assert ws2.receive_json()["slot"] == "TX2"


def test_broadcast_with_no_clients_is_a_no_op(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    # No websocket connected -- must not raise.
    audit.log_event("manual_stop", slot="TX1")
