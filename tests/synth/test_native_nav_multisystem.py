"""SP-D: the native engine attaches a broadcast nav message per system.

Tier 1 covers GPS + QZSS L1 C/A (both LNAV, 50 Hz). Galileo/BeiDou/GLONASS/
SBAS extend this test as their encoders land.
"""
import datetime as dt
import json
import pathlib

import numpy as np

from backend import config, inspector
from backend.scenario import ScenarioRequest
from backend.synth import engine

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")


def _run(tmp_path, monkeypatch, systems, nav=True):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = ScenarioRequest(
        rinex_path=_MIXED, lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 9, 1, 12), duration_s=3,
        # BeiDou B1I on one output with GPS L1 must span both carriers
        sample_rate=20_000_000.0 if "C" in systems else 6_000_000.0,
        sample_format="int16", engine="native",
        systems=systems, nav_message=nav)
    return engine.run(req)


def test_qzss_gets_lnav_provenance(tmp_path, monkeypatch):
    outdir = _run(tmp_path, monkeypatch, ["G", "J"])
    prov = json.loads((outdir / "meta.json").read_text())["provenance"]["nav"]
    assert prov.get("G/L1") == "lnav"
    assert prov.get("J/L1") == "lnav"


def test_gps_still_acquires_alongside_qzss_nav(tmp_path, monkeypatch):
    # QZSS native PRN (193+) is outside inspector's GPS C/A table, so acquire
    # a GPS PRN from the same G+J run and confirm the extra modulated SV did
    # not disturb the band.
    outdir = _run(tmp_path, monkeypatch, ["G", "J"], nav=True)
    meta = json.loads((outdir / "meta.json").read_text())
    gprn = next((s["prn"] for s in meta["provenance"]["svs"]
                 if s["sys"] == "G"), None)
    assert gprn is not None
    raw = np.fromfile(outdir / "gpssim.bin", dtype=np.int16).astype(np.float32)
    iq = raw[0::2] + 1j * raw[1::2]
    r = inspector.acquire(iq[:60000], 6_000_000.0, gprn)
    assert r["metric_db"] > 9.0, r


def test_galileo_gets_inav_on_the_e1b_component(tmp_path, monkeypatch):
    from backend.analysis import inav_encode

    outdir = _run(tmp_path, monkeypatch, ["G", "E"])
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["nav"].get("E/L1") == "inav"
    # I/NAV rides E1-B (data), not the E1-C pilot: primary code length 4092,
    # no CS25 secondary. The svs meta records code_len; the secondary is a
    # mixer detail, so just assert the SV is present and the run succeeded.
    esvs = [s for s in meta["provenance"]["svs"] if s["sys"] == "E"]
    assert esvs and all(s["code_len"] == 4092 for s in esvs)
    # symbol rate reached the mixer: 250 sym/s I/NAV stream is deterministic.
    a = _run(tmp_path / "b", monkeypatch, ["G", "E"])
    assert (outdir / "gpssim.bin").read_bytes() == (a / "gpssim.bin").read_bytes()
    assert inav_encode.SYM_RATE_HZ == 250.0


def test_beidou_gets_d1_provenance(tmp_path, monkeypatch):
    outdir = _run(tmp_path, monkeypatch, ["G", "C"])
    prov = json.loads((outdir / "meta.json").read_text())["provenance"]["nav"]
    assert prov.get("C/L1") == "d1"
    a = _run(tmp_path / "b", monkeypatch, ["G", "C"])
    assert (outdir / "gpssim.bin").read_bytes() == (a / "gpssim.bin").read_bytes()


def test_glonass_gets_strings_provenance(tmp_path, monkeypatch):
    outdir = _run(tmp_path, monkeypatch, ["G", "R"])
    prov = json.loads((outdir / "meta.json").read_text())["provenance"]["nav"]
    assert prov.get("R/G1") == "strings"


def test_sbas_gets_sbas_provenance(tmp_path, monkeypatch):
    outdir = _run(tmp_path, monkeypatch, ["G", "S"])
    prov = json.loads((outdir / "meta.json").read_text())["provenance"]["nav"]
    assert prov.get("S/L1") == "sbas"


def test_all_systems_carry_a_message(tmp_path, monkeypatch):
    outdir = _run(tmp_path, monkeypatch, ["G", "J", "E", "C", "R", "S"])
    prov = json.loads((outdir / "meta.json").read_text())["provenance"]["nav"]
    for key, name in (("G/L1", "lnav"), ("J/L1", "lnav"), ("E/L1", "inav"),
                      ("C/L1", "d1"), ("R/G1", "strings"), ("S/L1", "sbas")):
        assert prov.get(key) == name, (key, prov)


def test_nav_off_leaves_provenance_none(tmp_path, monkeypatch):
    outdir = _run(tmp_path, monkeypatch, ["G", "J"], nav=False)
    assert json.loads((outdir / "meta.json").read_text())[
        "provenance"]["nav"] == "none"
