import datetime as dt
import pathlib

import numpy as np

from backend import config, inspector, scenario
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def _run(tmp_path, monkeypatch, fading):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = scenario.ScenarioRequest(
        rinex_path=_RINEX, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2024, 1, 1), duration_s=8,
        sample_rate=2_600_000.0, sample_format="int16",
        engine="native", fading=fading)
    return engine.run(req)


def _metrics(outdir, offset):
    iq = inspector.read_iq(outdir / "gpssim.bin", "int16",
                           max_samples=int(2_600_000.0 * 0.010),
                           offset_samples=offset)
    out = {}
    for prn in range(1, 33):
        r = inspector.acquire(iq, 2_600_000.0, prn)
        if r["metric_db"] > 12.0:
            out[prn] = r["metric_db"]
    return out


def test_fading_spreads_per_sv_metrics(tmp_path, monkeypatch):
    flat = _metrics(_run(tmp_path, monkeypatch, {"model": "off"}), 0)
    faded = _metrics(_run(tmp_path, monkeypatch,
                          {"model": "lognormal", "sigma_db": 4.0,
                           "coherence_s": 2.0, "seed": 3}), 0)
    shared = set(flat) & set(faded)
    assert len(shared) >= 4
    flat_spread = np.std([flat[p] for p in shared])
    faded_spread = np.std([faded[p] for p in shared])
    assert faded_spread > flat_spread + 1.0


def test_fading_metric_changes_along_the_file(tmp_path, monkeypatch):
    outdir = _run(tmp_path, monkeypatch,
                  {"model": "lognormal", "sigma_db": 4.0, "coherence_s": 1.0, "seed": 9})
    early = _metrics(outdir, 0)
    late = _metrics(outdir, int(2_600_000.0 * 5.0))
    shared = set(early) & set(late)
    assert any(abs(early[p] - late[p]) > 1.0 for p in shared)
