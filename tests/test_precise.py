import json
import math
import pathlib

import numpy as np
import pytest

from backend.ephem import ephemeris, precise
from backend import geometry
from backend.gpstime import GPSTime
from backend.ephem.precise import (
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
    # satellites() now returns tuple keys (system, prn) instead of bare PRNs
    sats = sp3.satellites()
    assert all(isinstance(s, tuple) and len(s) == 2 for s in sats)
    assert [s[1] for s in sats if s[0] == "G"] == TRUTH["prns"]  # GPS satellites
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
    from backend.ephem import precise as _p

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
    from backend.ephem.precise import download_sp3, PreciseProductError
    with pytest.raises(PreciseProductError):
        download_sp3(WEEK, 4, tmp_path, [])


# --- merge_sp3 ------------------------------------------------------
def test_merge_sp3_dedupes_and_extends():
    import dataclasses

    from backend.ephem.precise import merge_sp3, parse_sp3
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
    from backend.ephem.precise import merge_sp3, parse_sp3
    a = parse_sp3(SP3)
    assert merge_sp3([a]) is a


def test_download_sp3_ultra_rapid_templating_and_cache_tiering(tmp_path, monkeypatch):
    from backend.ephem import precise as _p

    raw = SP3.read_bytes()
    seen = []

    class _Resp:
        def __init__(self, body): self.content = body
        def raise_for_status(self): pass

    import requests as _rq

    def _fake_get(url, timeout=0):
        seen.append(url)
        if "IGS0OPSULT" not in url:            # only ultra-rapid exists
            raise _rq.RequestException("404")
        return _Resp(raw)

    monkeypatch.setattr(_rq, "get", _fake_get)
    mirrors = [
        "https://x/{gpsweek}/IGS0OPSRAP_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz",
        "https://x/{gpsweek}/IGS0OPSULT_{yyyy}{doy}{hh}00_02D_15M_ORB.SP3.gz",
    ]
    out = _p.download_sp3(WEEK, 4, tmp_path, mirrors)
    assert seen[-1].endswith("0000_02D_15M_ORB.SP3.gz")   # {hh} -> "00"
    # fetched files now carry a coverage tag (_G / _GRECJ); this fixture is
    # GPS-only so the ultra-rapid product lands as ..._ULT_G.sp3.
    assert pathlib.Path(out).name == f"IGS_{WEEK:04d}_4_ULT_G.sp3"

    # a manually cached final product under the legacy un-tagged name still
    # wins over the ULT cache on the probe path
    (tmp_path / f"IGS_{WEEK:04d}_4_FIN.sp3").write_bytes(raw)
    again = _p.download_sp3(WEEK, 4, tmp_path, mirrors)
    assert pathlib.Path(again).name == f"IGS_{WEEK:04d}_4_FIN.sp3"


# --- multi-GNSS support (Task 20) ----------------------------------------
# Build test SP3 data with properly formatted P records (14-char right-aligned fields)
def _build_sp3_mgex():
    lines = [
        "#dP2026  9  1  0  0  0.00000000      96 ORBIT IGb14 HLM  GFZ",
        "## 2434 259200.00000000   900.00000000 60849 0.0000000000000",
        "*  2026  9  1  0  0  0.00000000",
    ]
    # Format: PG01 + 14-char X + 14-char Y + 14-char Z + 14-char clk
    records = [
        ("G", 1, -12345.678901, -9876.543210, 21000.111222, 123.456789),
        ("E", 11, 15000.000000, 20000.000000, -5000.000000, -50.000000),
        ("C", 6, -8000.000000, 35000.000000, 100.000000, 10.000000),
        ("R", 7, 19550.540000, 5000.000000, 15000.000000, -30.000000),
        ("J", 2, -30000.000000, 20000.000000, 35000.000000, 7.000000),
        ("S", 20, 42000.000000, 0.000000, 0.000000, 1.000000),
    ]
    for sys, prn, x, y, z, clk in records:
        line = f"P{sys}{prn:02d}{x:14.6f}{y:14.6f}{z:14.6f}{clk:14.6f}"
        lines.append(line)
    lines.append("EOF")
    return "\n".join(lines) + "\n"


_SP3_MGEX = _build_sp3_mgex()


def test_parse_sp3_keeps_grecj_drops_sbas():
    p = precise.parse_sp3(_SP3_MGEX, source="mgex-test")
    assert set(p.systems()) == {"G", "E", "C", "R", "J"}
    assert ("E", 11) in p.records and ("S", 20) not in p.records


# --- coverage-aware download_sp3 (Defect 1, no network) -----------------
def test_sp3_systems_detects_grecj_vs_gps_only():
    from backend.ephem.precise import _sp3_systems

    assert _sp3_systems(SP3.read_bytes()) == {"G"}          # fixture is GPS-only
    mgex = _sp3_systems(_SP3_MGEX.encode())
    assert "G" in mgex and mgex - {"G"}                     # has R/E/C/J too
    assert "S" not in mgex


class _Resp:
    def __init__(self, body): self.content = body
    def raise_for_status(self): pass


def test_download_sp3_want_multignss_prefers_grecj_over_gps_only_mirror(
        tmp_path, monkeypatch):
    import gzip
    from backend.ephem import precise as _p
    import requests as _rq

    gps_only = SP3.read_bytes()
    grecj = _SP3_MGEX.encode()

    def _fake_get(url, timeout=0):
        if "MGEX" in url:
            return _Resp(gzip.compress(grecj))
        return _Resp(gps_only)

    monkeypatch.setattr(_rq, "get", _fake_get)
    mirrors = [
        "https://a/{gpsweek}/IGS0OPSRAP_{yyyy}{doy}.sp3",       # GPS-only, first
        "https://b/{gpsweek}/GFZ0MGEXRAP_{yyyy}{doy}.SP3.gz",   # GRECJ, second
    ]
    out = _p.download_sp3(WEEK, 4, tmp_path, mirrors, want_multignss=True)
    assert pathlib.Path(out).name.endswith("_GRECJ.sp3")
    assert set(_p.parse_sp3(pathlib.Path(out)).systems()) >= {"G", "E", "C"}

    # want_multignss=False keeps the old "first plausible SP3 wins" behaviour
    out2 = _p.download_sp3(WEEK, 5, tmp_path, mirrors, want_multignss=False)
    assert pathlib.Path(out2).name.endswith("_G.sp3")


def test_download_sp3_want_multignss_does_not_serve_stale_gps_only_cache(
        tmp_path, monkeypatch):
    import gzip
    from backend.ephem import precise as _p
    import requests as _rq

    # a legacy un-tagged cache file for this day, GPS-only content
    (tmp_path / f"IGS_{WEEK:04d}_4.sp3").write_bytes(SP3.read_bytes())

    def _fake_get(url, timeout=0):
        return _Resp(gzip.compress(_SP3_MGEX.encode()))

    monkeypatch.setattr(_rq, "get", _fake_get)
    mirrors = ["https://b/{gpsweek}/WUM0MGEXRAP_{yyyy}{doy}.SP3.gz"]
    out = _p.download_sp3(WEEK, 4, tmp_path, mirrors, want_multignss=True)
    assert pathlib.Path(out).name.endswith("_GRECJ.sp3")
    assert "R" in set(_p.parse_sp3(pathlib.Path(out)).systems())

    # the freshly cached GRECJ product is now the best hit on the probe path
    again = _p.download_sp3(WEEK, 4, tmp_path, mirrors)
    assert pathlib.Path(again).name.endswith("_GRECJ.sp3")

    # a lone GPS-only legacy cache (no GRECJ sibling) is still served when
    # multi-GNSS is not requested
    (tmp_path / f"IGS_{WEEK:04d}_6.sp3").write_bytes(SP3.read_bytes())
    leg = _p.download_sp3(WEEK, 6, tmp_path, mirrors)
    assert pathlib.Path(leg).name == f"IGS_{WEEK:04d}_6.sp3"
