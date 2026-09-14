"""End-to-end GLONASS L2OF: the native engine emits the FDMA G2 band, the
receiver blind-scans the 14 FDMA channels, demodulates the 100 sym/s
meander stream, decodes strings via Hamming-sync (no fixed preamble),
reconstructs each slot's broadcast state vector and solves a position --
with no ephemeris handed in.

``brdc_mixed.rnx`` carries only 3 real GLONASS satellites (R01-R03), not
enough for a standalone 4-SV fix, so this fixture also carries 4 synthetic
ones (R04-R07) placed at distinct az/el from the receiver in (45, 30, 100)
(see the fixture's own R04-R07 records) -- all 7 turn out visible from
there at the chosen epoch, giving plenty of margin. Every SV uses a
distinct FDMA channel number (glo_k) so the 14-channel blind scan never
mixes two satellites on one carrier.

``glo_str_encode.nav_stream`` always starts its 15-string cycle at string
1 (``build_string``'s loop begins at i=0 -> sidx=1), so strings 1-4 always
land in the capture's first 6.8 s regardless of scenario epoch -- see the
L2/L5 design doc (SP-5, GLONASS L2OF).
"""
import datetime as dt
import json
import pathlib

import pytest

from backend import config
from backend.analysis import receiver
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import engine

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")
_RX = (45.0, 30.0, 100.0)
_FS = 10_000_000.0          # fs_policy's own G2 floor for ks=range(-7, 7)
_DUR = 16


@pytest.fixture(scope="module")
def glo_l2of_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("glo_l2of_fix")
    _orig = config.OUT_DIR
    config.OUT_DIR = out
    try:
        start = dt.datetime(2026, 9, 1, 12, 0, 0)
        req = ScenarioRequest(
            rinex_path=_MIXED, lat=_RX[0], lon=_RX[1], alt=_RX[2], start=start,
            duration_s=_DUR, sample_rate=_FS, sample_format="int16",
            engine="native", systems=("R",), bands=["L2"], nav_message=True)
        outdir = engine.run(req)
    finally:
        config.OUT_DIR = _orig
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    _week, sow = ephemeris.gps_week_and_sow(gps_start)
    return outdir, sow


def test_meta_records_strings_on_g2(glo_l2of_run):
    outdir, _sow = glo_l2of_run
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["nav"].get("R/G2") == "strings"
    ids = {b["id"] for b in meta["bands"]}
    assert "G2" in ids
    g2 = next(b for b in meta["bands"] if b["id"] == "G2")
    assert g2["centre_hz"] == 1_246_000_000.0
    assert (outdir / "gpssim_g2.bin").exists()


def test_closed_loop_fix_from_strings_decoded_out_of_native_g2_iq(glo_l2of_run):
    outdir, sow = glo_l2of_run
    res = receiver.fix_from_iq(
        outdir / "gpssim_g2.bin", "int16", _FS,
        eph_by_prn={}, approx_time_gps=sow, marker_llh=_RX, band="G2")

    assert "error" not in res, res
    assert sum(1 for v in res["nav_decode"].values() if v == "ok") >= 4
    assert res["error_m"] < 200.0
    assert res["residual_rms_m"] < 100.0
