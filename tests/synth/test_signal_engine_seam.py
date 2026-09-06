import pytest

from backend import scenario
from backend.synth import signal_engine


def test_default_engine_routes_to_gps_sdr_sim(monkeypatch):
    called = {}
    monkeypatch.setattr("backend.generator.run",
                        lambda req, progress_cb=None, binary=None: called.setdefault("gss", req))
    req = scenario.ScenarioRequest(rinex_path="x", lat=0, lon=0, alt=0,
                                   start=__import__("datetime").datetime(2024, 1, 1),
                                   duration_s=1)
    signal_engine.run(req)
    assert "gss" in called


def test_native_engine_routes_to_synth(monkeypatch):
    called = {}
    monkeypatch.setattr("backend.synth.engine.run",
                        lambda req, progress_cb=None: called.setdefault("native", req))
    req = scenario.ScenarioRequest(rinex_path="x", lat=0, lon=0, alt=0,
                                   start=__import__("datetime").datetime(2024, 1, 1),
                                   duration_s=1, engine="native")
    signal_engine.run(req)
    assert "native" in called


def test_unknown_engine_raises():
    req = scenario.ScenarioRequest(rinex_path="x", lat=0, lon=0, alt=0,
                                   start=__import__("datetime").datetime(2024, 1, 1),
                                   duration_s=1, engine="bogus")
    with pytest.raises(ValueError):
        signal_engine.run(req)
