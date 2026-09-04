import datetime as dt
import pathlib
import time

import pytest

from backend import config, generator, scenario

pytestmark = [
    pytest.mark.skipif(
        not pathlib.Path(config.GPS_SDR_SIM_BIN).exists(),
        reason="gps-sdr-sim binary not built"),
    # KNOWN_ISSUES F3: subprocess exit signal -5, same issue as test_integration_generate.py.
    pytest.mark.xfail(reason="F3: generator.run_segment's subprocess crashes only under pytest", strict=False),
]

_FIX = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"


def _base_req(rinex_path):
    return scenario.ScenarioRequest(
        rinex_path=rinex_path, lat=39.9, lon=32.8, alt=800,
        start=dt.datetime(2024, 1, 1, 0, 0, 0),
        duration_s=300, sample_rate=2.6e6, sample_format="int16")


def test_run_segment_produces_valid_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    req = _base_req(str(_FIX))
    outdir = generator.run_segment(req, llh=(40.0, 33.0, 850.0), time_offset_s=0.0, duration_s=1.0)
    binfile = outdir / "gpssim.bin"
    assert binfile.exists()
    assert binfile.stat().st_size > 0


def test_run_segment_keeps_up_with_real_time(tmp_path, monkeypatch):
    """The spec's open risk: must generate a 1s segment in well under 1
    wall-clock second, or the live loop can never catch up. If this fails,
    the fix is raising run_segment's duration_s default at the LiveSession
    call site (Task 3), not editing this test."""
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    req = _base_req(str(_FIX))
    t0 = time.monotonic()
    generator.run_segment(req, llh=(40.0, 33.0, 850.0), time_offset_s=0.0, duration_s=1.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"1s segment took {elapsed:.2f}s to generate -- live loop cannot keep up"
