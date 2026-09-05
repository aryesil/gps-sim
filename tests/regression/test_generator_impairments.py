"""generator.run wiring for the impairment layer + IQ integrity report."""
import datetime as dt
import json
import pathlib
import stat
import textwrap

import numpy as np
import pytest

from backend import generator, scenario

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"


def _fake_binary(tmp_path):
    p = tmp_path / "fake_sim.py"
    p.write_text(textwrap.dedent('''
        import sys
        import numpy as np
        out = sys.argv[sys.argv.index("-o") + 1]
        n = 40000
        t = np.arange(n)
        iq = np.zeros(2 * n, dtype=np.int16)
        iq[0::2] = (3000 * np.cos(2 * np.pi * 1000 * t / 2.6e6)).astype(np.int16)
        iq[1::2] = (3000 * np.sin(2 * np.pi * 1000 * t / 2.6e6)).astype(np.int16)
        iq.tofile(out)
    '''))
    sh = tmp_path / "fake_sim"
    sh.write_text(f'#!/usr/bin/env bash\nexec python "{p}" "$@"\n')
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
    return str(sh)


def _req(**kw):
    base = dict(rinex_path=str(FIX), lat=41.0, lon=29.0, alt=100.0,
                start=dt.datetime(2026, 8, 28, 12, 0, 0), duration_s=4,
                sample_rate=2.6e6)
    base.update(kw)
    return scenario.ScenarioRequest(**base)


def test_clean_run_is_untouched_and_gets_an_integrity_report(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    outdir = generator.run(_req(), binary=_fake_binary(tmp_path))
    meta = json.loads((outdir / "meta.json").read_text())

    assert meta["provenance"]["impairments"] is None
    assert not (outdir / "gpssim.clean.bin").exists()
    ig = meta["iq_integrity"]
    assert ig["ok"] is True, ig["problems"]
    assert ig["n_samples"] > 0


def test_impairments_rewrite_bin_and_keep_clean_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    req = _req(impairments={"enabled_flag": True, "cfo_hz": 1200.0,
                            "snr_db": 6.0}, random_seed=7)
    outdir = generator.run(req, binary=_fake_binary(tmp_path))
    meta = json.loads((outdir / "meta.json").read_text())

    rep = meta["provenance"]["impairments"]
    assert rep is not None
    assert set(rep["applied"]) == {"cfo_hz", "awgn"}
    assert rep["seed"] == 7
    assert rep["clean_output"] == "gpssim.clean.bin"

    clean = np.fromfile(outdir / "gpssim.clean.bin", dtype=np.int16)
    dirty = np.fromfile(outdir / "gpssim.bin", dtype=np.int16)
    assert clean.shape == dirty.shape
    assert not np.array_equal(clean, dirty)


def test_impairments_are_deterministic_across_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    b = _fake_binary(tmp_path)
    spec = dict(impairments={"enabled_flag": True, "dc_i": 0.05,
                             "snr_db": 10.0}, random_seed=3)
    a = generator.run(_req(**spec), binary=b)
    c = generator.run(_req(**spec), binary=b)
    xa = np.fromfile(a / "gpssim.bin", dtype=np.int16)
    xc = np.fromfile(c / "gpssim.bin", dtype=np.int16)
    assert np.array_equal(xa, xc)


def test_disabled_impairments_dict_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    req = _req(impairments={"enabled_flag": False, "cfo_hz": 1200.0})
    outdir = generator.run(req, binary=_fake_binary(tmp_path))
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["impairments"] is None
    assert not (outdir / "gpssim.clean.bin").exists()
