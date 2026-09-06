import datetime as dt
import json
import pathlib

import numpy as np

from backend import config
from backend.scenario import ScenarioRequest
from backend.synth import _lib, engine

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")
_GPS2 = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def test_two_bands_write_two_files(tmp_path):
    code = _lib.code(0, 5, 1023)[0].astype(np.int8)
    sv = _lib.one_sv_spec(code, carrier_hz=1000.0, code_phase0=10.0)
    b1 = _lib.BandSpec()
    _lib.fill_band(b1, str(tmp_path / "L1.bin"), 2_600_000.0, 2, 26000, [sv])
    b2 = _lib.BandSpec()
    _lib.fill_band(b2, str(tmp_path / "G1.bin"), 16_000_000.0, 2, 160000, [sv])
    rc = _lib.run_bands([b1, b2])
    assert rc == 0
    assert (tmp_path / "L1.bin").stat().st_size == 26000 * 4  # int16 IQ
    assert (tmp_path / "G1.bin").stat().st_size == 160000 * 4


def test_abi_version_is_16():
    assert _lib.load_lib().synth_abi_version() == _lib.ABI_VERSION == 16


# --- part 2: engine.run multi-band output + meta band map -------------------

def test_gps_only_run_is_single_band_backcompat(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = ScenarioRequest(rinex_path=_GPS2, lat=41.0, lon=29.0, alt=100.0,
                          start=dt.datetime(2024, 1, 1), duration_s=3,
                          sample_rate=2_600_000.0, sample_format="int16",
                          engine="native")            # systems defaults ("G",)
    outdir = engine.run(req)
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["output"] == "gpssim.bin"
    assert [b["id"] for b in meta["bands"]] == ["L1"]
    assert meta["bands"][0]["systems"] == ["G"]
    assert (outdir / "gpssim.bin").exists()
    assert not (outdir / "gpssim_g1.bin").exists()


def test_glonass_adds_a_second_band_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    # noon: brdc_mixed only carries G01-G03, visible at (41,29) mid-day; at
    # midnight no GPS SV clears the mask and the L1 band would be empty.
    req = ScenarioRequest(rinex_path=_MIXED, lat=41.0, lon=29.0, alt=100.0,
                          start=dt.datetime(2026, 9, 1, 12), duration_s=2,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "R"])
    outdir = engine.run(req)
    meta = json.loads((outdir / "meta.json").read_text())
    ids = {b["id"] for b in meta["bands"]}
    assert ids == {"L1", "G1"}
    g1 = next(b for b in meta["bands"] if b["id"] == "G1")
    assert g1["systems"] == ["R"]
    assert (outdir / g1["file"]).exists()
    assert g1["file"] == "gpssim_g1.bin"
    assert g1["fs"] >= 8_900_000.0
    # meta.json still keeps the Phase-1 L1 top-level keys
    assert meta["output"] == "gpssim.bin"
    assert any(s["sys"] == "R" for s in meta["provenance"]["svs"])
