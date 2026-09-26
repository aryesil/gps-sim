"""Receiver clock, multipath and RF impairments on the native engine.

They used to exist only on the gps-sdr-sim path; a native request carrying
them silently produced a clean signal."""
import datetime as dt
import json
import pathlib

import numpy as np
import pytest

from backend import config, inspector, scenario
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")
_FS = 2_600_000.0
_L1 = 1_575_420_000.0
_CHIP = 1.023e6


def _run(tmp_path, monkeypatch, name, **kw):
    out = tmp_path / name
    out.mkdir()
    monkeypatch.setattr(config, "OUT_DIR", out)
    req = scenario.ScenarioRequest(
        rinex_path=_RINEX, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2024, 1, 1), duration_s=1, sample_rate=_FS,
        sample_format="int16", engine="native", **kw)
    outdir = engine.run(req)
    return outdir, json.loads((outdir / "meta.json").read_text())


def _knots(tmp_path, monkeypatch, name, **kw):
    """Trajectory knots of every satellite in one run, in call order."""
    got = []
    orig = engine._trajectory_knots

    def spy(*a, **k):
        got.append(orig(*a, **k))
        return got[-1]
    monkeypatch.setattr(engine, "_trajectory_knots", spy)
    _run(tmp_path, monkeypatch, name, **kw)
    monkeypatch.setattr(engine, "_trajectory_knots", orig)
    return got


def test_receiver_clock_rides_every_satellite(tmp_path, monkeypatch):
    bias, drift = 1e-6, 2e-8
    clk = {"model": "poly", "bias_s": bias, "drift_s_per_s": drift,
           "ref_epoch_s": 0.0}
    base = _knots(tmp_path, monkeypatch, "a")
    withc = _knots(tmp_path, monkeypatch, "b", receiver_clock=clk,
                   models_to_iq=True)
    assert len(base) == len(withc) > 4
    for (cf0, _p0, cr0, kp0), (cf1, _p1, cr1, kp1) in zip(base, withc):
        # drift: every carrier Doppler moves by -f*drift, code rate by -chip*drift
        assert np.allclose(np.array(cf1) - np.array(cf0), -_L1 * drift, atol=1e-3)
        assert np.allclose(np.array(cr1) - np.array(cr0), -_CHIP * drift, atol=1e-6)
        # the offset grows by drift*t: relative code phase slips accordingly
        t_last = (len(kp0) - 1) * engine._BLOCK_SAMPLES / _FS
        slip = (kp1[-1] - kp1[0]) - (kp0[-1] - kp0[0])
        assert slip == pytest.approx(-_CHIP * drift * t_last, abs=1e-6)


def test_receiver_clock_bias_delays_code_on_all_prns(tmp_path, monkeypatch):
    clk = {"model": "poly", "bias_s": 2e-6}
    a, _ = _run(tmp_path, monkeypatch, "a")
    b, meta = _run(tmp_path, monkeypatch, "b", receiver_clock=clk, models_to_iq=True)
    ia = inspector.read_iq(a / "gpssim.bin", "int16", max_samples=int(_FS * 0.01))
    ib = inspector.read_iq(b / "gpssim.bin", "int16", max_samples=int(_FS * 0.01))
    shifts = []
    for prn in range(1, 33):
        ra, rb = inspector.acquire(ia, _FS, prn), inspector.acquire(ib, _FS, prn)
        if ra["metric_db"] > 12 and rb["metric_db"] > 12:
            d = (rb["code_phase_chips"] - ra["code_phase_chips"] + 511.5) % 1023 - 511.5
            shifts.append(abs(d))
    assert len(shifts) >= 4
    # 2 us = 2.046 chips; acquisition resolves ~0.4 chip at 2.6 Msps
    assert np.all(np.abs(np.array(shifts) - 2.046) < 0.5), shifts
    rc = meta["provenance"]["channel_models"]["receiver_clock"]
    assert rc["clock_offset_s"] == pytest.approx(2e-6)


def test_receiver_clock_needs_models_to_iq(tmp_path, monkeypatch):
    a, _ = _run(tmp_path, monkeypatch, "a")
    b, meta = _run(tmp_path, monkeypatch, "b",
                   receiver_clock={"model": "poly", "bias_s": 2e-6})
    assert np.array_equal(np.fromfile(a / "gpssim.bin", np.int16),
                          np.fromfile(b / "gpssim.bin", np.int16))
    assert meta["provenance"]["channel_models"] is None


def test_multipath_and_impairments_reach_native_iq(tmp_path, monkeypatch):
    a, _ = _run(tmp_path, monkeypatch, "a")
    b, meta = _run(
        tmp_path, monkeypatch, "b", models_to_iq=True,
        multipath={"model": "specular", "reflections": [
            {"excess_delay_m": 100.0, "amplitude": 0.5}]},
        impairments={"enabled_flag": True, "snr_db": 0.0, "cfo_hz": 50.0},
        random_seed=3)
    xa = np.fromfile(a / "gpssim.bin", np.int16)
    xb = np.fromfile(b / "gpssim.bin", np.int16)
    assert xa.size == xb.size and not np.array_equal(xa, xb)
    prov = meta["provenance"]
    assert prov["channel_models"]["multipath"]["L1"]["n_reflections"] == 1
    imp = prov["impairments"]["L1"]
    assert imp["seed"] == 3 and "awgn" in imp["applied"] and "cfo_hz" in imp["applied"]
    assert not list(b.glob("*.tmp"))
