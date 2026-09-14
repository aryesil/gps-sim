"""Galileo E5a-I rides the L5 band IQ out of the native engine: the E5a-I
code (fixed ICD memory code, not LFSR-generated) acquires and the F/NAV
data modulation is present and deterministic."""
import datetime as dt
import json
import pathlib

from backend import config, inspector
from backend.scenario import ScenarioRequest
from backend.synth import engine
from tests.synth import _corr

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")
# 10.23 Mcps BPSK: main lobe +/- 10.23 MHz, same floor as GPS L5 (see
# test_native_l5_nav_in_iq.py).
_FS = 25_000_000.0


def _req(dur=6, nav_message=True):
    return ScenarioRequest(
        rinex_path=_MIXED, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 9, 1, 6), duration_s=dur,
        sample_rate=_FS, sample_format="int8", engine="native",
        systems=("E",), bands=["L5"], nav_message=nav_message,
        # req.sample_rate only governs the L1 band; pin the L5 band's own
        # fs explicitly (else fs_policy.band_floor gives 20.5 Msps here,
        # not this module's intended 25 Msps floor).
        l5_sample_rate=_FS)


def test_meta_records_fnav_on_e5a(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    m = json.loads((engine.run(_req()) / "meta.json").read_text())
    assert m["provenance"]["nav"].get("E/L5") == "fnav"
    assert "GAL_E5AI" in m["provenance"]["signals"]
    assert m["output"] == "gpssim_l5.bin"


def test_e5a_i_acquires_in_the_native_l5_iq(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    outdir = engine.run(_req(dur=6))
    meta = json.loads((outdir / "meta.json").read_text())
    prns = [s["prn"] for s in meta["provenance"]["svs"] if s["sys"] == "E"]
    assert prns
    iq = inspector.read_iq(outdir / "gpssim_l5.bin", "int8",
                           max_samples=int(_FS * 0.050))
    hits = 0
    for prn in prns:
        r = _corr.acquire(iq, _FS, "E_E5AI", prn, code_len=10230,
                          chip_hz=10_230_000.0, dopp_hz=6000.0,
                          dopp_step=200.0, nperiods=1)
        if r["metric_db"] > 10.0:
            hits += 1
    assert hits >= min(2, len(prns))


def test_e5a_iq_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    a = engine.run(_req(dur=4))
    b = engine.run(_req(dur=4))
    assert (a / "gpssim_l5.bin").read_bytes() == (b / "gpssim_l5.bin").read_bytes()
