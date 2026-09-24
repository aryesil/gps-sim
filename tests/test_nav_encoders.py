"""SP-D tier 1: nav-message dispatch + QZSS L1 C/A reuse of the GPS encoder."""
import math

import numpy as np
import pytest

from backend.analysis import lnav_decode, nav_encoders
from backend.ephem import ephemeris
from backend.synth import signals

_RINEX = "tests/fixtures/brdc_mixed.rnx"
_WEEK, _SOW = 2325, 561600.0     # arbitrary aligned epoch for encode


@pytest.fixture(scope="module")
def eph_multi():
    e = ephemeris.parse_rinex_multi(_RINEX, ("G", "J", "E", "C", "R", "S"),
                                    require=())
    return ephemeris.align_epochs(e, _WEEK, _SOW)


def _first(eph, sysc):
    for k, v in eph.items():
        if isinstance(k, tuple) and k[0] == sysc:
            return v
    raise AssertionError(f"no {sysc} record in fixture")


@pytest.mark.parametrize("sysc", ["G", "J"])
def test_lnav_systems_return_50hz_pm1_stream(eph_multi, sysc):
    rec = _first(eph_multi, sysc)
    sig = signals.signal_for(sysc)
    res = nav_encoders.nav_stream_for(sysc, sig, rec, {}, _WEEK, _SOW, 6)
    assert res is not None
    arr, rate = res
    assert rate == 50.0
    assert arr.dtype == np.int8
    assert set(np.unique(arr)).issubset({-1, 1})
    assert len(arr) == 50 * (math.ceil(6) + 30)


def test_qzss_stream_deframes_and_decodes(eph_multi):
    rec = _first(eph_multi, "J")
    arr, _rate = nav_encoders.nav_stream_for("J", signals.signal_for("J"),
                                             rec, {}, _WEEK, _SOW, 30)
    bits = (arr > 0).astype(np.int8)
    sfs = {h["subframe_id"]: h["words"] for h in lnav_decode.find_frame(bits)}
    assert {1, 2, 3} <= set(sfs)
    dec = lnav_decode.decode_ephemeris(sfs)
    assert dec["e"] == pytest.approx(rec["e"], abs=1e-9)
    assert dec["sqrtA"] == pytest.approx(rec["sqrtA"], abs=1e-3)
    assert dec["m0"] == pytest.approx(rec["m0"], abs=1e-8)


def test_galileo_returns_250hz_inav_stream(eph_multi):
    rec = _first(eph_multi, "E")
    res = nav_encoders.nav_stream_for("E", signals.signal_for("E"), rec, {},
                                      _WEEK, _SOW, 6)
    assert res is not None
    arr, rate = res
    assert rate == 250.0
    assert set(np.unique(arr)).issubset({-1, 1})


@pytest.mark.parametrize("sysc,rate", [("C", 50.0), ("R", 100.0), ("S", 500.0)])
def test_other_systems_return_their_rate(eph_multi, sysc, rate):
    rec = _first(eph_multi, sysc)
    res = nav_encoders.nav_stream_for(sysc, signals.signal_for(sysc), rec,
                                      {}, _WEEK, _SOW, 6, prn=30)
    assert res is not None
    arr, r = res
    assert r == rate
    assert set(np.unique(arr)).issubset({-1, 1})


def test_l2c_signal_dispatches_to_cnav(eph_multi):
    from backend.synth import signals
    rec = _first(eph_multi, "G")
    res = nav_encoders.nav_stream_for(
        "G", signals.SIGNALS["GPS_L2C"], rec, {}, _WEEK, _SOW, 12, prn=1)
    assert res is not None
    arr, rate = res
    assert rate == 50.0
    assert set(np.unique(arr)).issubset({-1, 1})
    assert len(arr) % 600 == 0


def test_l1_dispatch_still_lnav(eph_multi):
    from backend.synth import signals
    rec = _first(eph_multi, "G")
    arr, rate = nav_encoders.nav_stream_for(
        "G", signals.signal_for("G"), rec, {}, _WEEK, _SOW, 12)
    assert rate == 50.0


def test_unknown_system_returns_none(eph_multi):
    assert nav_encoders.nav_stream_for("X", signals.signal_for("G"), {},
                                       {}, _WEEK, _SOW, 6) is None


# A live session restarts every nav stream each ~1 s segment, so a stream
# started later must carry exactly the symbols the earlier stream carries at
# the same time: message/page/subframe schedules follow GNSS time, not the
# stream start. The first 12 symbols are skipped: a continuous rate-1/2 K=7
# FEC (CNAV, SBAS) restarts from the zero state, a transient the engine's
# 1 s nav lead keeps off the air.
_FEC_SKIP = 12


@pytest.mark.parametrize("sysc,sig_name", [
    ("G", "GPS_L1CA"), ("J", "QZSS_L1CA"), ("E", "GAL_E1"), ("C", "BDS_B1I"),
    ("S", "SBAS_L1"), ("G", "GPS_L2C"), ("G", "GPS_L5I"), ("E", "GAL_E5AI"),
    ("C", "BDS_B2AD"), ("I", "IRNSS_L5"), ("R", "GLO_G1"),
])
def test_nav_stream_is_time_invariant(sysc, sig_name):
    e = ephemeris.parse_rinex_multi(_RINEX, ("G", "J", "E", "C", "R", "S", "I"),
                                    require=())
    e = ephemeris.align_epochs(e, _WEEK, _SOW)
    key = next((k for k in sorted(e, key=str) if isinstance(k, tuple) and k[0] == sysc), None)
    if key is None:
        pytest.skip(f"no {sysc} record in fixture")
    rec, prn = e[key], key[1]
    sig = signals.SIGNALS[sig_name]
    t0 = _SOW + 7.3
    a, rate = nav_encoders.nav_stream_for(sysc, sig, rec, {}, _WEEK, t0, 200, prn=prn)
    ta = nav_encoders.stream_t0_sow(sysc, sig, t0, prn=prn)
    for d in (1.0, 6.0, 60.0):
        b, _ = nav_encoders.nav_stream_for(sysc, sig, rec, {}, _WEEK, t0 + d, 20, prn=prn)
        tb = nav_encoders.stream_t0_sow(sysc, sig, t0 + d, prn=prn)
        off = int(round((tb - ta) * rate))
        n = min(b.size, a.size - off)
        assert np.array_equal(a[off + _FEC_SKIP:off + n], b[_FEC_SKIP:n]), f"shift {d} s"
