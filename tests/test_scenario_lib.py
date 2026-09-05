# tests/test_scenario_lib.py
import pytest
from fastapi.testclient import TestClient

from backend import scenario_lib
from backend.app import app

client = TestClient(app)


def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_lib, "_DIR", tmp_path / "scenarios")
    params = {"lat": 41.0, "lon": 29.0, "alt": 100.0, "duration_s": 300,
              "sample_rate": 2.6e6, "sample_format": "int16", "iq_path": "/should/be/dropped"}
    scenario_lib.save("istanbul-static", params)
    loaded = scenario_lib.load("istanbul-static")
    assert loaded["lat"] == 41.0
    assert loaded["sample_format"] == "int16"
    assert "iq_path" not in loaded  # not in the field allowlist
    assert "istanbul-static" in scenario_lib.list_names()


def test_save_rejects_empty_params(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_lib, "_DIR", tmp_path / "scenarios")
    with pytest.raises(scenario_lib.ScenarioLibError):
        scenario_lib.save("empty", {"iq_path": "/only/disallowed/field"})


def test_load_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_lib, "_DIR", tmp_path / "scenarios")
    with pytest.raises(scenario_lib.ScenarioLibError):
        scenario_lib.load("does-not-exist-xyz")


def test_invalid_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_lib, "_DIR", tmp_path / "scenarios")
    with pytest.raises(scenario_lib.ScenarioLibError):
        scenario_lib.save("../escape", {"lat": 1.0})


def test_api_save_list_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_lib, "_DIR", tmp_path / "scenarios")
    r = client.post("/api/scenario/save", json={
        "name": "api-test", "params": {"lat": 1.0, "lon": 2.0}})
    assert r.status_code == 200
    assert r.json() == {"saved": "api-test"}

    r = client.get("/api/scenario/list")
    assert "api-test" in r.json()["names"]

    r = client.get("/api/scenario/load", params={"name": "api-test"})
    assert r.json()["params"] == {"lat": 1.0, "lon": 2.0}

    r = client.get("/api/scenario/load", params={"name": "nope"})
    assert r.status_code == 404
