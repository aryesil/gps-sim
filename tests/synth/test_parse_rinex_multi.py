# tests/synth/test_parse_rinex_multi.py
import pathlib

import pytest

from backend.ephem import ephemeris

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
    assert r["system"] == "R" and "x_m" in r and "vx" in r


def test_multi_gps_only_matches_facade():
    a = ephemeris.parse_rinex_multi(_MIXED, ("G",))
    assert all(isinstance(k, int) for k in a)


def test_missing_system_raises_named():
    with pytest.raises(ephemeris.EphemerisUnavailable) as ei:
        ephemeris.parse_rinex_multi(_GPS2, systems=("E",))
    assert "E" in str(ei.value)


def test_require_lets_absent_optional_system_be_dropped():
    # GPS-only file, ask for G+E but only require G -> E is dropped, no raise
    eph = ephemeris.parse_rinex_multi(_GPS2, systems=("G", "E"),
                                      require=("G",))
    assert eph and {(k[0] if isinstance(k, tuple) else "G") for k in eph} == {"G"}


def test_require_still_raises_when_a_required_system_is_absent():
    with pytest.raises(ephemeris.EphemerisUnavailable):
        ephemeris.parse_rinex_multi(_GPS2, systems=("G", "E"),
                                    require=("G", "E"))
