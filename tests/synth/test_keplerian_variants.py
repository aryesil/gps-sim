# tests/synth/test_keplerian_variants.py
import datetime as dt
import pathlib

import numpy as np

from backend import ephemeris, geometry

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def _gps_eph():
    week, sow = ephemeris.gps_week_and_sow(dt.datetime(2024, 1, 1, 0, 0, 18))
    return ephemeris.align_epochs(ephemeris.parse_rinex(_RINEX), week, sow), sow


def test_sys_params_table():
    for s in ("G", "J", "E", "C"):
        p = geometry.SYS_PARAMS[s]
        assert p["mu"] > 3.9e14 and p["mu"] < 4.0e14
        assert 7.29e-5 < p["omega_e_dot"] < 7.30e-5
    # BeiDou CGCS2000 constants differ from GPS
    assert geometry.SYS_PARAMS["C"]["mu"] == 3.986004418e14
    assert geometry.SYS_PARAMS["C"]["omega_e_dot"] == 7.2921150e-5


def test_keplerian_state_matches_sat_state_for_gps():
    eph, sow = _gps_eph()
    prn = sorted(eph)[0]
    f = geometry.keplerian_state(eph[prn])          # no "system" -> GPS
    p_new, v_new, c_new = f(sow + 123.0)
    p_old, v_old, c_old = geometry.sat_state(eph[prn], sow + 123.0)
    assert np.allclose(p_new, p_old, atol=1e-6)
    assert np.allclose(v_new, v_old, atol=1e-9)
    assert abs(c_new - c_old) < 1e-15


def test_cpp_matches_python_for_each_kepler_system():
    from backend.synth import _lib, engine

    _MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")
    eph = ephemeris.parse_rinex_multi(_MIXED, ("G", "E", "C", "J"))
    # propagation sys-int: 0 GPS/QZSS, 1 Galileo, 2 BeiDou MEO/IGSO, 3 BeiDou GEO
    sysmap = {"G": 0, "J": 0, "E": 1, "C": 2}
    for (s, prn), rec in eph.items():
        st = engine.kepler_struct(rec)
        t = rec["toe"] + 300.0
        p_c, v_c, _c_c = _lib.sat_state_sys(st, sysmap[s], t)
        p_p, v_p, _c_p = geometry.sat_state(rec, t)
        assert np.max(np.abs(np.array(p_c) - p_p)) < 1e-3, (s, prn)
        assert np.max(np.abs(np.array(v_c) - v_p)) < 1e-4, (s, prn)


def test_beidou_geo_rotation_applied_for_low_inclination():
    eph, sow = _gps_eph()
    base = dict(eph[sorted(eph)[0]])
    geo = dict(base); geo["system"] = "C"; geo["i0"] = 0.05      # GEO-like
    meo = dict(base); meo["system"] = "C"; meo["i0"] = 0.96      # MEO-like
    pg = geometry.keplerian_state(geo)(sow + 3600.0)[0]
    pm = geometry.keplerian_state(meo)(sow + 3600.0)[0]
    # the GEO branch adds a -OMEGA_E_DOT*tk rotation about X; result differs
    assert not np.allclose(pg, pm)
