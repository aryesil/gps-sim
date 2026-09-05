# tests/test_audit.py
from fastapi.testclient import TestClient

from backend import audit, config
from backend.app import app

client = TestClient(app)


def test_log_event_appends_jsonl_line(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    audit.log_event("manual_stop", slot="TX1")
    events = audit.read_events()
    assert len(events) == 1
    assert events[0]["event"] == "manual_stop"
    assert events[0]["slot"] == "TX1"
    assert "ts" in events[0]


def test_read_events_newest_first_and_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    for i in range(5):
        audit.log_event("live_start", slot=str(i))
    events = audit.read_events(limit=3)
    assert len(events) == 3
    assert [e["slot"] for e in events] == ["4", "3", "2"]


def test_read_events_empty_when_no_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    assert audit.read_events() == []


def test_api_audit_endpoint_returns_logged_events(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    audit.log_event("transmit_start", slot="TX2", iq_path="/x/gpssim.bin")
    r = client.get("/api/audit")
    assert r.status_code == 200
    body = r.json()
    assert body["events"][0]["event"] == "transmit_start"
    assert body["events"][0]["slot"] == "TX2"
