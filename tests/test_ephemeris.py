# tests/test_ephemeris.py
import datetime as dt
import gzip
import pathlib

import pytest

from backend import ephemeris

FIX = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"


def test_parse_rinex_returns_prn_keyed_ephemeris():
    eph = ephemeris.parse_rinex(FIX)
    assert len(eph) >= 6
    prn = sorted(eph)[0]
    e = eph[prn]
    for k in ("toe", "sqrtA", "e", "m0", "delta_n", "omega", "omega0",
              "omega_dot", "i0", "idot", "cuc", "cus", "crc", "crs",
              "cic", "cis", "af0", "af1", "af2", "tgd"):
        assert isinstance(e[k], float)
    assert 5150.0 < e["sqrtA"] < 5160.0        # GPS semi-major axis sqrt, metres^0.5
    assert 0.0 <= e["e"] < 0.03


def test_get_ephemeris_uses_cache_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(ephemeris.config, "DATA_DIR", tmp_path)
    (tmp_path / "rinex").mkdir()
    date = dt.date(2026, 9, 3)
    ephemeris.save_uploaded_rinex(date, FIX.read_bytes())
    eph = ephemeris.get_ephemeris(date, download=False)
    assert len(eph) >= 6


def test_get_ephemeris_raises_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(ephemeris.config, "DATA_DIR", tmp_path)
    (tmp_path / "rinex").mkdir()
    with pytest.raises(ephemeris.EphemerisUnavailable):
        ephemeris.get_ephemeris(dt.date(2000, 1, 1), download=False)
