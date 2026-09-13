"""GPS L5 rides the L5 band IQ out of the native engine: the I5 code
acquires and the CNAV data modulation is present and deterministic."""
import datetime as dt
import json
import pathlib

from backend import config, inspector
from backend.scenario import ScenarioRequest
from backend.synth import engine
from tests.synth import _corr

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")
# 10.23 Mcps BPSK: main lobe +/- 10.23 MHz. One coherent I5 period clears
# ~13 dB at 25 Msps; below ~20 Msps the truncated lobe drops it into the
# noise, so the L5 tests run at the band's ~25 Msps floor.
_FS = 25_000_000.0


def _req(dur=6, nav_message=True):
    return ScenarioRequest(
        rinex_path=_RINEX, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2021, 12, 11, 11, 59, 42), duration_s=dur,
        sample_rate=_FS, sample_format="int8", engine="native",
        systems=("G",), bands=["L5"], nav_message=nav_message)


def test_meta_records_cnav_on_l5(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    m = json.loads((engine.run(_req()) / "meta.json").read_text())
    assert m["provenance"]["nav"].get("G/L5") == "cnav"
    assert "GPS_L5I" in m["provenance"]["signals"]
    assert m["output"] == "gpssim_l5.bin"


def test_l5_i5_acquires_in_the_native_l5_iq(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    outdir = engine.run(_req(dur=6))
    meta = json.loads((outdir / "meta.json").read_text())
    prns = [s["prn"] for s in meta["provenance"]["svs"] if s["sys"] == "G"]
    iq = inspector.read_iq(outdir / "gpssim_l5.bin", "int8",
                           max_samples=int(_FS * 0.050))
    hits = 0
    for prn in prns:
        r = _corr.acquire(iq, _FS, "G_L5I", prn, code_len=10230,
                          chip_hz=10_230_000.0, dopp_hz=6000.0,
                          dopp_step=200.0, nperiods=1)
        if r["metric_db"] > 10.0:
            hits += 1
    assert hits >= min(4, len(prns))


def test_l5_iq_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    a = engine.run(_req(dur=4))
    b = engine.run(_req(dur=4))
    assert (a / "gpssim_l5.bin").read_bytes() == (b / "gpssim_l5.bin").read_bytes()
