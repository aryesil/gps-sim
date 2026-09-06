# tests/synth/test_parse_rinex_multi.py
import pathlib

import pytest

from backend import ephemeris

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")
_GPS2 = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def test_parse_rinex_facade_unchanged():
    a = ephemeris.parse_rinex(_GPS2)
    assert all(isinstance(k, int) for k in a)
    assert "sqrtA" in next(iter(a.values()))


def test_multi_returns_tuple_keys_and_system_tag():
    eph = ephemeris.parse_rinex_multi(_MIXED, systems=("G", "E", "C", "R", "J", "S"))
    syskeys = {k[0] for k in eph}
    assert {"G", "E", "C", "R"} <= syskeys
    g = next(v for k, v in eph.items() if k[0] == "G")
    assert g["system"] == "G" and "sqrtA" in g
    r = next(v for k, v in eph.items() if k[0] == "R")
    assert r["system"] == "R" and "x_km" in r and "vx" in r


def test_multi_gps_only_matches_facade():
    a = ephemeris.parse_rinex_multi(_MIXED, ("G",))
    assert all(isinstance(k, int) for k in a)


def test_missing_system_raises_named():
    with pytest.raises(ephemeris.EphemerisUnavailable) as ei:
        ephemeris.parse_rinex_multi(_GPS2, systems=("E",))
    assert "E" in str(ei.value)
