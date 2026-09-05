import datetime as dt
import json
import os
import pathlib
import stat
import textwrap

import pytest

from backend import generator, scenario

_FIX = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"


def test_parse_progress_reads_time_line():
    assert generator.parse_progress("Time into run = 15.0", 30) == pytest.approx(0.5)
    assert generator.parse_progress("Using UTC time ...", 30) is None


def _fake_binary(tmp_path):
    """A stand-in for gps-sdr-sim: prints progress, writes the -o file."""
    p = tmp_path / "fake_sim.py"
    p.write_text(textwrap.dedent('''
        import sys
        out = sys.argv[sys.argv.index("-o") + 1]
        for t in (0.0, 5.0, 10.0):
            print(f"Time into run = {t}", flush=True)
        open(out, "wb").write(b"\\x00\\x01" * 1000)
    '''))
    sh = tmp_path / "fake_sim"
    sh.write_text(f'#!/usr/bin/env bash\nexec python "{p}" "$@"\n')
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
    return str(sh)


def test_run_creates_output_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    fake = _fake_binary(tmp_path)
    req = scenario.ScenarioRequest(
        rinex_path=str(_FIX), lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 9, 3, 6, 0, 0), duration_s=10)
    seen = []
    outdir = generator.run(req, progress_cb=seen.append, binary=fake)
    assert (outdir / "gpssim.bin").stat().st_size == 2000
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["sample_rate"] == req.sample_rate
    assert meta["output"] == "gpssim.bin"
    assert seen and seen[-1] == pytest.approx(1.0, abs=0.34)


def test_run_raises_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    bad = tmp_path / "bad"
    bad.write_text('#!/usr/bin/env bash\necho boom >&2\nexit 3\n')
    bad.chmod(0o755)
    req = scenario.ScenarioRequest(
        rinex_path=str(_FIX), lat=1, lon=2, alt=3,
        start=dt.datetime(2026, 1, 1), duration_s=1)
    with pytest.raises(generator.GeneratorError) as ei:
        generator.run(req, binary=str(bad))
    assert "boom" in str(ei.value)


def _capture_segment_argv(tmp_path, monkeypatch, time_offset_s):
    """Run run_segment with the nav write + subprocess stubbed out, and
    return the argv it would have executed. Argv-level so this does not
    depend on gps-sdr-sim running (KNOWN_ISSUES F3 makes that xfail under
    pytest)."""
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    monkeypatch.setattr(generator, "_prepare_nav",
                        lambda req, outdir, time_offset_s=0.0: outdir / "nav.rinex2.n")
    captured = {}
    monkeypatch.setattr(generator, "_run_gps_sdr_sim",
                        lambda argv, duration_s, progress_cb=None: captured.update(argv=argv))
    req = scenario.ScenarioRequest(
        rinex_path=str(_FIX), lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 1, 1, 12, 0, 0), duration_s=300)
    generator.run_segment(req, llh=(40.0, 33.0, 850.0),
                          time_offset_s=time_offset_s, duration_s=1.0, binary="fake")
    return captured["argv"]


def test_run_segment_shifts_scenario_start_with_time_offset(tmp_path, monkeypatch):
    """time_offset_s must move gps-sdr-sim's -t timestamp, not just the nav
    file's toc/toe: shifting only the epoch leaves tk = t - toe nonzero and
    corrupts satellite geometry instead of shifting time."""
    base = _capture_segment_argv(tmp_path, monkeypatch, 0.0)
    shifted = _capture_segment_argv(tmp_path, monkeypatch, 30.0)
    t_base = base[base.index("-t") + 1]
    t_shifted = shifted[shifted.index("-t") + 1]
    fmt = "%Y/%m/%d,%H:%M:%S"
    assert (dt.datetime.strptime(t_shifted, fmt)
            - dt.datetime.strptime(t_base, fmt)) == dt.timedelta(seconds=30)


def test_run_segment_passes_no_extra_offset_to_prepare_nav(tmp_path, monkeypatch):
    """The nav epoch is shifted via seg_req.start alone; passing the offset
    to _prepare_nav as well would double-shift the ephemeris."""
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    seen = {}
    def fake_prepare_nav(req, outdir, time_offset_s=0.0):
        seen["start"] = req.start
        seen["offset"] = time_offset_s
        return outdir / "nav.rinex2.n"
    monkeypatch.setattr(generator, "_prepare_nav", fake_prepare_nav)
    monkeypatch.setattr(generator, "_run_gps_sdr_sim",
                        lambda argv, duration_s, progress_cb=None: None)
    req = scenario.ScenarioRequest(
        rinex_path=str(_FIX), lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 1, 1, 12, 0, 0), duration_s=300)
    generator.run_segment(req, llh=(40.0, 33.0, 850.0), time_offset_s=45.0,
                          duration_s=1.0, binary="fake")
    assert seen["offset"] == 0.0
    assert seen["start"] == dt.datetime(2026, 1, 1, 12, 0, 45)


def test_channel_models_off_by_default_no_prechannel_file(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    fake = _fake_binary(tmp_path)
    req = scenario.ScenarioRequest(
        rinex_path=str(_FIX), lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 9, 3, 6, 0, 0), duration_s=10,
        multipath={"model": "specular",
                   "reflections": [{"excess_delay_m": 40.0, "amplitude": 0.5}]})
    outdir = generator.run(req, binary=fake)
    assert not (outdir / "gpssim.prechannel.bin").exists()
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["channel_models"] is None


def test_channel_models_to_iq_writes_prechannel_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    fake = _fake_binary(tmp_path)
    req = scenario.ScenarioRequest(
        rinex_path=str(_FIX), lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 9, 3, 6, 0, 0), duration_s=10,
        models_to_iq=True,
        receiver_clock={"model": "poly", "bias_s": 1e-6},
        multipath={"model": "specular",
                   "reflections": [{"excess_delay_m": 60.0, "amplitude": 0.4}]})
    outdir = generator.run(req, binary=fake)
    assert (outdir / "gpssim.prechannel.bin").stat().st_size == 2000
    assert (outdir / "gpssim.bin").exists()
    rep = json.loads((outdir / "meta.json").read_text())["provenance"]["channel_models"]
    assert rep["multipath"]["n_reflections"] == 1
    assert rep["receiver_clock"]["range_bias_m"] != 0.0


def test_frac_delay_handles_advance_and_delay():
    import numpy as np
    x = np.arange(10, dtype=np.complex64)
    d = generator._frac_delay(x, 2.0)
    assert d[0] == 0 and d[2] == 0 and d[5] == 3
    a = generator._frac_delay(x, -2.0)
    assert a[0] == 2 and a[6] == 8 and a[8] == 0
