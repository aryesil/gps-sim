# tests/test_recording.py
import pathlib

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.obs import recording
from backend.app import app

client = TestClient(app)


def test_writer_appends_events_with_relative_t(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    w = recording.RecordingWriter("TX1")
    w.append({"progress": 1})
    w.append({"finished": True})
    w.close()
    events = recording.read_events(w.name)
    assert len(events) == 2
    assert events[0]["progress"] == 1
    assert "t" in events[0] and events[0]["t"] >= 0
    assert events[1]["finished"] is True
    assert w.name in recording.list_names()


def test_read_events_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        recording.read_events("does-not-exist-xyz")


def test_list_names_empty_when_no_recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    assert recording.list_names() == []


def test_live_start_with_record_flag_creates_replayable_recording(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    fixture = pathlib.Path(__file__).parents[1] / "fixtures" / "brdc_sample.rnx"
    r = client.post("/api/live/start", json={
        "rinex_path": str(fixture), "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2024-01-01T00:00:00", "confirm_isolated": True,
        "dry_run": True, "duration_s": 3600, "max_duration_s": 0.1,
        "record": True})
    assert r.status_code == 200
    assert '"finished": true' in r.text

    names = client.get("/api/recording/list").json()["names"]
    assert len(names) >= 1
    name = names[-1]

    replay = client.get(f"/api/recording/replay?name={name}&speed=1000")
    assert replay.status_code == 200
    assert '"finished": true' in replay.text


def test_recording_replay_unknown_name_404():
    r = client.get("/api/recording/replay?name=does-not-exist-xyz")
    assert r.status_code == 404


def test_read_events_skips_a_truncated_last_line(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    w = recording.RecordingWriter("TX1")
    w.append({"progress": 1})
    w.close()
    with open(w.path, "a") as f:
        f.write('{"t": 2.0, "progr')            # yarıda kesilmiş satır
    assert [e["progress"] for e in recording.read_events(w.name)] == [1]


def test_writer_survives_a_write_error_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    w = recording.RecordingWriter("TX1")

    class _Full:
        def write(self, *_):
            raise OSError("disk full")
        def flush(self): pass
        def close(self): pass
    w._f = _Full()
    w.append({"a": 1})            # fırlatmamalı
    w.append({"a": 2})            # kapalıyken no-op
    w.close()


def test_api_keys_non_dict_json_is_ignored(monkeypatch):
    monkeypatch.setenv("API_KEYS_JSON", '["not", "a", "dict"]')
    assert config._api_keys() == {}
