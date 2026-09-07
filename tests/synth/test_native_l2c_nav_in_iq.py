"""GPS L2C rides the L2 band IQ out of the native engine: CM code acquires
and the CNAV data modulation is present and deterministic."""
import datetime as dt
import json
import pathlib

import numpy as np

from backend import config, inspector
from backend.scenario import ScenarioRequest
from backend.synth import engine
from tests.synth import _corr

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def _req(dur=6, nav_message=True):
    return ScenarioRequest(
        rinex_path=_RINEX, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2021, 12, 11, 11, 59, 42), duration_s=dur,
        sample_rate=2_600_000.0, sample_format="int16", engine="native",
        systems=("G",), bands=["L2"], nav_message=nav_message)


def test_meta_records_cnav_on_l2(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    m = json.loads((engine.run(_req()) / "meta.json").read_text())
    assert m["provenance"]["nav"].get("G/L2") == "cnav"
    assert "GPS_L2C" in m["provenance"]["signals"]
    assert m["output"] == "gpssim_l2.bin"


def test_l2c_cm_acquires_in_the_native_l2_iq(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    outdir = engine.run(_req(dur=6))
    meta = json.loads((outdir / "meta.json").read_text())
    prns = [s["prn"] for s in meta["provenance"]["svs"] if s["sys"] == "G"]
    iq = inspector.read_iq(outdir / "gpssim_l2.bin", "int16",
                           max_samples=int(2_600_000.0 * 0.100))
    hits = 0
    for prn in prns:
        r = _corr.acquire(iq, 2_600_000.0, "G_L2C", prn,
                          code_len=10230, chip_hz=511_500.0,
                          dopp_hz=6000.0, dopp_step=200.0)
        if r["metric_db"] > 12.0:
            hits += 1
    assert hits >= min(4, len(prns))


def test_l2_iq_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    a = engine.run(_req(dur=5))
    b = engine.run(_req(dur=5))
    assert (a / "gpssim_l2.bin").read_bytes() == (b / "gpssim_l2.bin").read_bytes()
