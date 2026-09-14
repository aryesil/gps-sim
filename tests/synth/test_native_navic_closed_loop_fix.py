"""End-to-end NavIC (IRNSS) L5-SPS: the native engine emits NavIC-only IQ
on the shared L5 RF band, the receiver blindly acquires the 1023-chip
L5-SPS code, decodes subframes 1/2 (ISRO-IRNSS-ICD-SPS-1.1) straight out
of the IQ, reconstructs each satellite's Keplerian ephemeris and solves
one joint position fix -- with no ephemeris handed in.

The fixture RINEX (brdc_mixed.rnx) carries 5 synthetic NavIC records
(I01..I05, GEO/IGSO-like: sqrtA ~ 6493.4 (semi-major axis ~42,164 km),
small eccentricity, varied RAAN/inclination/mean-anomaly) chosen so all
5 sit above 30 deg elevation from the receiver below -- NavIC is a
regional system, so an Indian-Ocean-area receiver is required for
visibility, unlike the other (global) constellations' fixtures.

Subframes 1 and 2 are each 12 s (600 symbols at 50 sps); the capture
start is chosen so TOWC is an exact multiple of 12, and the 30 s
duration guarantees at least one full subframe-1 + subframe-2 pair
appears per SV regardless of acquisition warm-up. The 1.023 Mcps chip
rate needs only the L5 band's own 2.1 Msps floor (2x chip rate,
rounded up) -- a short probe confirmed acquisition SNR is unchanged
from a higher rate (23-27 dB either way), and 30 s at 2.1 Msps keeps
this test's IQ file and decode-time memory well under what 6 Msps
would need for the same duration.
"""
import datetime as dt
import pathlib

import pytest

from backend import config
from backend.analysis import receiver
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import engine

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")
# Southern India -- picked (by scanning the fixture's own I01..I05 geometry)
# as a point where all 5 synthetic NavIC SVs sit comfortably above 25 deg.
_RX = (13.0, 77.0, 900.0)
_FS = 2_100_000.0
_START = dt.datetime(2026, 8, 31, 3, 0, 30)
_DUR = 30


@pytest.fixture(scope="module")
def navic_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("navic_fix")
    _orig = config.OUT_DIR
    config.OUT_DIR = out
    try:
        req = ScenarioRequest(
            rinex_path=_MIXED, lat=_RX[0], lon=_RX[1], alt=_RX[2],
            start=_START, duration_s=_DUR, sample_rate=_FS,
            sample_format="int8", engine="native", systems=("I",),
            bands=["L5"], nav_message=True,
            # req.sample_rate only governs the L1 band; pin the L5 band's
            # own fs explicitly (it already equals fs_policy.band_floor
            # for NavIC alone -- see module docstring) so generation and
            # the receiver's decode call below agree on the actual fs.
            l5_sample_rate=_FS)
        outdir = engine.run(req)
    finally:
        config.OUT_DIR = _orig
    gps_start = _START + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    _week, sow = ephemeris.gps_week_and_sow(gps_start)
    return outdir, sow


def test_meta_records_navic_on_l5(navic_run):
    import json
    outdir, _sow = navic_run
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["nav"].get("I/L5") == "navic"


def test_closed_loop_fix_from_subframes_decoded_out_of_native_l5_iq(navic_run):
    outdir, sow = navic_run
    res = receiver.fix_from_iq(
        outdir / "gpssim_l5.bin", "int8", _FS,
        eph_by_prn={}, approx_time_gps=sow, marker_llh=_RX, band="L5")

    assert "error" not in res, res
    assert sum(1 for v in res["nav_decode"].values() if v == "ok") >= 4
    assert all(p.startswith("I") for p in res["prns_used"]), res["prns_used"]
    assert res["error_m"] < 200.0
    assert res["residual_rms_m"] < 120.0
