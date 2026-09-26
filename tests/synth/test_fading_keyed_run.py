"""Keyed fading through the native engine: the key never reaches meta.json
unless asked for, and a run without a caller key gets a fresh one."""
import datetime as dt
import json
import pathlib

import numpy as np

from backend import config, scenario
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")
_KEY = bytes(range(32)).hex()


def _run(tmp_path, monkeypatch, fading, name):
    out = tmp_path / name
    out.mkdir()
    monkeypatch.setattr(config, "OUT_DIR", out)
    req = scenario.ScenarioRequest(
        rinex_path=_RINEX, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2024, 1, 1), duration_s=1,
        sample_rate=2_600_000.0, sample_format="int16",
        engine="native", fading=fading)
    outdir = engine.run(req)
    meta = json.loads((outdir / "meta.json").read_text())
    iq = np.fromfile(outdir / "gpssim.bin", dtype=np.int16)
    return meta, iq


def _fade(**kw):
    return {"model": "keyed", "sigma_db": 6.0, "coherence_s": 0.2, **kw}


def test_keyed_run_hides_the_key_by_default(tmp_path, monkeypatch):
    meta, _ = _run(tmp_path, monkeypatch, _fade(key=_KEY), "a")
    assert meta["provenance"]["fading"] == "keyed"
    svs = meta["provenance"]["svs"]
    assert svs and all(s["fading_model"] == 2 for s in svs)
    assert all("fading_key" not in s for s in svs)
    assert len({s["fading_key_id"] for s in svs}) == 1
    assert _KEY not in json.dumps(meta)


def test_keyed_run_records_the_key_when_asked(tmp_path, monkeypatch):
    meta, _ = _run(tmp_path, monkeypatch, _fade(key=_KEY, record_key=True), "a")
    assert {s["fading_key"] for s in meta["provenance"]["svs"]} == {_KEY}


def test_same_key_same_iq_fresh_key_different_iq(tmp_path, monkeypatch):
    _, a = _run(tmp_path, monkeypatch, _fade(key=_KEY), "a")
    _, b = _run(tmp_path, monkeypatch, _fade(key=_KEY), "b")
    m1, c = _run(tmp_path, monkeypatch, _fade(), "c")
    m2, d = _run(tmp_path, monkeypatch, _fade(), "d")
    assert np.array_equal(a, b)
    assert not np.array_equal(c, d)
    assert (m1["provenance"]["svs"][0]["fading_key_id"]
            != m2["provenance"]["svs"][0]["fading_key_id"])
