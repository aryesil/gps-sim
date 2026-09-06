import datetime as dt
import pathlib
import time

from backend import config, scenario
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def test_native_generates_30s_gps_only_under_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = scenario.ScenarioRequest(
        rinex_path=_RINEX, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2024, 1, 1), duration_s=30,
        sample_rate=2_600_000.0, sample_format="int16", engine="native")
    t0 = time.perf_counter()
    engine.run(req)
    dt_s = time.perf_counter() - t0
    # Loose ceiling: catches accidental O(n^2) / per-sample libm regressions.
    # Not a performance claim. Any speed comparison lives in the spec as a
    # separate measurement, never asserted here.
    assert dt_s < 30.0
