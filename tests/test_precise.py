import json
import math
import pathlib

import numpy as np
import pytest

from backend import ephemeris, geometry
from backend.gpstime import GPSTime
from backend.precise import (
    PreciseEphemerisProvider, PreciseProductParseError, EpochOutOfCoverage,
    InterpolationWindowError, SatelliteNotInProduct, parse_sp3, _neville_clean,
)

FIXDIR = pathlib.Path(__file__).parent / "fixtures"
SP3 = FIXDIR / "igs_sample.sp3"
TRUTH = json.loads((FIXDIR / "igs_sample_truth.json").read_text())
BRDC = FIXDIR / "brdc_sample.rnx"

WEEK = TRUTH["gps_week"]
TOE = TRUTH["toe_sow"]
INTERVAL = TRUTH["interval_s"]


@pytest.fixture(scope="module")
def provider():
    p = PreciseEphemerisProvider()
    p.load(SP3)
    return p


@pytest.fixture(scope="module")
def eph():
    return ephemeris.parse_rinex(BRDC)


# --- parsing --------------------------------------------------------------
def test_parse_basic_header_and_records():
    sp3 = parse_sp3(SP3)
    assert sp3.gps_week == WEEK
    assert sp3.epoch_interval_s == pytest.approx(INTERVAL)
    assert sp3.satellites() == TRUTH["prns"]
    assert len(sp3.epoch_times) == TRUTH["n_epochs"]


def test_parse_rejects_non_sp3():
    with pytest.raises(PreciseProductParseError):
        parse_sp3("not an sp3 file\njust text\n")


def test_parse_rejects_sp3_with_no_gps_records():
    text = "#dP2026  8 28  0  0  0.00000000       2\n" \
           "##  2433      0.00000000    900.00000000  58000 0.0\n" \
           "*  2026  8 28  0  0  0.00000000\n" \
           "PR01  1000.0  2000.0  3000.0  0.0\n" \
           "EOF\n"
    with pytest.raises(PreciseProductParseError):
        parse_sp3(text)


# --- lookup failures ----------------------------------------------------
def test_missing_satellite(provider):
    with pytest.raises(SatelliteNotInProduct):
        provider.get_state(30, GPSTime(WEEK, TOE))


def test_epoch_before_coverage(provider):
    lo, _ = provider.coverage()
    with pytest.raises(EpochOutOfCoverage):
        provider.get_state(1, lo.shifted(-1.0))


def test_epoch_after_coverage(provider):
    _, hi = provider.coverage()
    with pytest.raises(EpochOutOfCoverage):
        provider.get_state(1, hi.shifted(1.0))


def test_near_boundary_requires_opt_in(provider):
    lo, _ = provider.coverage()
    near = lo.shifted(INTERVAL * 1.5)   # inside coverage, <5 samples from start
    with pytest.raises(InterpolationWindowError):
        provider.get_state(1, near)
    st = provider.get_state(1, near, allow_boundary=True)
    assert "offcentre" in st.source


# --- interpolation accuracy vs the Kepler truth the fixture was built from
@pytest.mark.parametrize("prn", [1, 4, 7, 10])
def test_position_interpolation_matches_kepler_plus_bias(provider, eph, prn):
    b = TRUTH["bias_by_prn"][str(prn)]
    bias = np.array([b["dx_m"], b["dy_m"], b["dz_m"]])
    sow = TOE + 7.5 * INTERVAL + 123.0            # off-grid epoch, mid-arc
    st = provider.get_state(prn, GPSTime(WEEK, sow))
    truth_pos, truth_vel, _ = geometry.sat_state(eph[prn], sow)
    err = np.linalg.norm(np.array(st.position_ecef_m) - (truth_pos + bias))
    assert err < 0.05                              # < 5 cm mid-arc
    verr = np.linalg.norm(np.array(st.velocity_ecef_mps) - truth_vel)
    assert verr < 5e-3                             # < 5 mm/s


def test_clock_interpolation_matches_baked_linear(provider, eph):
    prn = 3
    b = TRUTH["bias_by_prn"][str(prn)]
    sow = TOE + 10.0 * INTERVAL + 200.0
    _, _, clk_kepler = geometry.sat_state(eph[prn], sow)
    expected = clk_kepler + b["clk_c0_s"] + b["clk_c1_sps"] * (sow - TOE)
    st = provider.get_state(prn, GPSTime(WEEK, sow))
    # SP3 clock is quantised at write time to 1e-6 us formatting; loose tol.
    assert st.clock_bias_s == pytest.approx(expected, abs=5e-10)
    # drift is the slope of the (linearly interpolated) SP3 clock samples,
    # i.e. Kepler clock rate + baked c1 -- compare to a numeric difference.
    c0 = provider.get_state(prn, GPSTime(WEEK, sow - 30.0)).clock_bias_s
    c1 = provider.get_state(prn, GPSTime(WEEK, sow + 30.0)).clock_bias_s
    assert st.clock_drift_sps == pytest.approx((c1 - c0) / 60.0, abs=1e-13)


def test_velocity_matches_numeric_difference(provider):
    prn = 6
    sow = TOE + 5.0 * INTERVAL
    p0 = np.array(provider.get_state(prn, GPSTime(WEEK, sow - 0.5)).position_ecef_m)
    p1 = np.array(provider.get_state(prn, GPSTime(WEEK, sow + 0.5)).position_ecef_m)
    v = np.array(provider.get_state(prn, GPSTime(WEEK, sow)).velocity_ecef_mps)
    assert np.allclose(p1 - p0, v, rtol=0, atol=1e-3)


def test_state_fn_threads_scenario_week(provider):
    f = provider.state_fn(1, week=WEEK)
    pos, vel, clk = f(TOE + 3.0 * INTERVAL)
    assert len(pos) == 3 and len(vel) == 3
    assert np.linalg.norm(pos) > 2.5e7


def test_state_fn_wrong_week_fails_loudly(provider):
    f = provider.state_fn(1, week=WEEK + 1)
    with pytest.raises(EpochOutOfCoverage):
        f(TOE)


# --- the interpolator itself ------------------------------------------
def test_neville_exact_on_polynomial():
    xs = [-2.0, -1.0, 0.0, 1.0, 2.0, 3.0]
    f = lambda x: 3 * x ** 3 - 2 * x ** 2 + x - 7
    df = lambda x: 9 * x ** 2 - 4 * x + 1
    ys = [f(x) for x in xs]
    p, d = _neville_clean(xs, ys, 0.4)
    assert p == pytest.approx(f(0.4), rel=1e-9)
    assert d == pytest.approx(df(0.4), rel=1e-9)


# --- download_sp3 mirror templating (no network) ---------------------
def test_download_sp3_templating_and_gunzip(tmp_path, monkeypatch):
    import gzip
    from backend import precise as _p

    raw = SP3.read_bytes()
    seen = []

    class _Resp:
        def __init__(self, body): self.content = body
        def raise_for_status(self): pass

    def _fake_get(url, timeout=0):
        seen.append(url)
        if "IGS0OPSRAP" in url:                 # pretend rapid is missing
            raise _p_requests.RequestException("404")
        return _Resp(gzip.compress(raw))        # final, gzipped

    import requests as _p_requests
    monkeypatch.setattr(_p_requests, "get", _fake_get)

    mirrors = [
        "https://x/{gpsweek}/IGS0OPSRAP_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz",
        "https://y/{gps_week}/IGS0OPSFIN_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz",
    ]
    out = _p.download_sp3(WEEK, 4, tmp_path, mirrors)
    assert pathlib.Path(out).read_bytes()[:2] in (b"#c", b"#d", b"#a", b"#b")
    # 2433 * 7 + 4 days after 1980-01-06 -> a real calendar date; the year
    # and 3-digit doy must have been substituted into both URLs.
    assert seen[0].startswith("https://x/2433/IGS0OPSRAP_2026")
    assert seen[1].startswith("https://y/2433/IGS0OPSFIN_2026")
    assert "0000_01D_15M_ORB.SP3.gz" in seen[1]


def test_download_sp3_empty_mirrors_raises(tmp_path):
    from backend.precise import download_sp3, PreciseProductError
    with pytest.raises(PreciseProductError):
        download_sp3(WEEK, 4, tmp_path, [])


# --- merge_sp3 ------------------------------------------------------
def test_merge_sp3_dedupes_and_extends():
    import dataclasses

    from backend.precise import merge_sp3, parse_sp3
    a = parse_sp3(SP3)
    # a synthetic "next day": same tracks shifted +86400 s
    shift = 86400.0
    recs = {prn: [(t + shift, x, y, z, c) for (t, x, y, z, c) in rows]
            for prn, rows in a.records.items()}
    b = dataclasses.replace(a, source="day2.sp3", records=recs,
                            epoch_times=[t + shift for t in a.epoch_times])

    same = merge_sp3([a, parse_sp3(SP3)])
    assert len(same.epoch_times) == len(a.epoch_times)          # exact dupes collapse
    assert same.satellites() == a.satellites()
    assert same.source.startswith("merged(")

    joined = merge_sp3([a, b])
    assert len(joined.epoch_times) == 2 * len(a.epoch_times)
    lo, hi = joined.coverage_seconds
    assert hi - lo > a.coverage_seconds[1] - a.coverage_seconds[0]


def test_merge_sp3_single_passthrough():
    from backend.precise import merge_sp3, parse_sp3
    a = parse_sp3(SP3)
    assert merge_sp3([a]) is a
