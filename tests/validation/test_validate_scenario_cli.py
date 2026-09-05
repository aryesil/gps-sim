"""scripts/validate_scenario.py -- the end-to-end validation chain."""
import datetime as dt
import importlib.util
import json
import pathlib
import stat
import textwrap

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
FIX = _ROOT / "tests" / "fixtures" / "brdc_sample.rnx"

_spec = importlib.util.spec_from_file_location(
    "validate_scenario", _ROOT / "scripts" / "validate_scenario.py")
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)

from backend import scenario  # noqa: E402


def _req(**kw):
    base = dict(rinex_path=str(FIX), lat=41.0082, lon=28.9784, alt=100.0,
                start=dt.datetime(2026, 2, 10, 6, 0, 0), duration_s=6,
                sample_rate=2.6e6)
    base.update(kw)
    return scenario.ScenarioRequest(**base)


def test_geometry_only_run_passes_and_has_a_budget():
    rep = vs.run_validation(_req(), do_generate=False)
    assert rep["ok"] is True
    assert rep["stages"]["geometry"]["status"] == "pass"
    assert rep["stages"]["geometry"]["worst_position_diff_m"] < 1e-2
    assert rep["stages"]["generation"]["status"] == "skip"
    assert rep["stages"]["error_budget"]["status"] == "pass"
    assert rep["stages"]["error_budget"]["uere_rms_m"] > 0


def test_bad_rinex_path_fails_fast():
    rep = vs.run_validation(_req(rinex_path="/no/such.rnx"), do_generate=False)
    assert rep["ok"] is False
    assert rep["stages"]["ephemeris"]["status"] == "fail"


def test_human_report_renders_all_stages():
    rep = vs.run_validation(_req(), do_generate=False)
    text = vs._human(rep)
    for name in ("ephemeris", "geometry", "generation", "error_budget", "OVERALL"):
        assert name in text


def test_full_chain_with_a_fake_binary(tmp_path, monkeypatch):
    # a stand-in gps-sdr-sim that writes a plausible-looking .bin
    p = tmp_path / "fake_sim.py"
    p.write_text(textwrap.dedent('''
        import sys, numpy as np
        out = sys.argv[sys.argv.index("-o") + 1]
        n = 60000
        t = np.arange(n)
        iq = np.zeros(2 * n, dtype=np.int16)
        iq[0::2] = (2000 * np.cos(2*np.pi*1000*t/2.6e6)).astype(np.int16)
        iq[1::2] = (2000 * np.sin(2*np.pi*1000*t/2.6e6)).astype(np.int16)
        iq.tofile(out)
    '''))
    sh = tmp_path / "fake_sim"
    sh.write_text(f'#!/usr/bin/env bash\nexec python "{p}" "$@"\n')
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)

    from backend import generator
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)

    rep = vs.run_validation(_req(), binary=str(sh), do_generate=True)
    assert rep["stages"]["generation"]["status"] == "pass"
    assert rep["stages"]["iq_integrity"]["status"] in ("pass", "warn")
    # the fake IQ has no real PRNs, so acquisition is allowed to fail --
    # what matters is the chain ran and produced a verdict
    assert "status" in rep["stages"]["acquisition"]
    assert rep["ok"] in (True, False)
    json.dumps(rep)                       # fully serialisable


def test_main_exit_code(capsys):
    rc = vs.main([str(FIX), "--lat", "41.0", "--lon", "29.0",
                  "--start", "2026-02-10T06:00:00", "--duration", "5",
                  "--no-generate", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
