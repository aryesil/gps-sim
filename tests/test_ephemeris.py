# tests/test_ephemeris.py
import datetime as dt
import gzip
import pathlib

import pytest

from backend.ephem import ephemeris

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


def _make_multi_epoch(tmp_path):
    """Single PRN (G08), three epochs at 04:00/12:00/20:00 UTC with matching toe."""
    lines = FIX.read_text().splitlines(keepends=True)
    hidx = next(i for i, l in enumerate(lines) if "END OF HEADER" in l)
    header = lines[: hidx + 1]
    bstart = next(i for i, l in enumerate(lines) if l.startswith("G08 2026 08 28 12 00 00"))
    block = lines[bstart : bstart + 8]

    def variant(hh, sow):
        b = list(block)
        b[0] = b[0][:15] + f"{hh:02d}" + b[0][17:]          # epoch hour field
        b[3] = b[3][:4] + f"{sow:19.12E}" + b[3][23:]        # orbit-3 line, Toe field
        return b

    out = list(header)
    for hh, sow in ((4, 446400.0), (12, 475200.0), (20, 504000.0)):
        out += variant(hh, sow)
    p = tmp_path / "multi.rnx"
    p.write_text("".join(out))
    return p


def test_parse_rinex_multi_epoch_selects_noon(tmp_path):
    eph = ephemeris.parse_rinex(_make_multi_epoch(tmp_path))
    assert set(eph) == {8}
    assert eph[8]["toe"] == pytest.approx(475200.0)


def test_parse_rinex_single_epoch_fixture_unchanged():
    eph = ephemeris.parse_rinex(FIX)
    assert len(eph) == 10
    assert eph[8]["toe"] == pytest.approx(475200.0)


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


@pytest.mark.parametrize("s,prn,ok", [
    ("C", 421, False),
    ("C", 6, True),
    ("E", 361, False),
    ("E", 36, True),
    ("S", 21, True),
    ("S", 141, True),
    ("J", 2, True),
    ("J", 241, False),
    ("J", 193, True),
    ("G", 0, False),
    ("G", 5, True),
])
def test_prn_in_range(s, prn, ok):
    assert ephemeris._prn_in_range(s, prn) is ok


def test_parse_rinex_multi_at_gps_selects_nearest_epoch(tmp_path):
    p = _make_multi_epoch(tmp_path)
    eph = ephemeris.parse_rinex_multi(p, ("G",), at_gps=dt.datetime(2026, 8, 28, 4, 30))
    assert eph[8]["toe"] == pytest.approx(446400.0)
    eph = ephemeris.parse_rinex_multi(p, ("G",), at_gps=dt.datetime(2026, 8, 28, 19, 0))
    assert eph[8]["toe"] == pytest.approx(504000.0)


def test_align_epochs_keeps_real_record_near_start():
    e = ephemeris.parse_rinex(FIX)[8]
    week = int(e["gps_week"])
    near = ephemeris.align_epochs({8: e}, week, e["toe"] + 4000.0,
                                  kepler_grid_s=3600.0, keep_real_within_s=7200.0)[8]
    assert near["toe"] == e["toe"] and near["toc"] == e["toc"]
    far = ephemeris.align_epochs({8: e}, week, e["toe"] + 10000.0,
                                 kepler_grid_s=3600.0, keep_real_within_s=7200.0)[8]
    assert far["toe"] == pytest.approx((e["toe"] + 10000.0) // 3600.0 * 3600.0)
    # off by default: always relabeled
    dflt = ephemeris.align_epochs({8: e}, week, e["toe"] + 4000.0, kepler_grid_s=3600.0)[8]
    assert dflt["toe"] != e["toe"]


def test_parse_rinex_keeps_records_with_blank_spare_fields(tmp_path):
    # Real-time BRDC files (BKG ConvertoCpp) write a record's trailing spare
    # fields as blanks. georinex sizes the fields from the satellite's first
    # record; when that one is full-width, a later blank-padded record fails
    # float() and is dropped entirely -- leaving only stale epochs.
    lines = _make_multi_epoch(tmp_path).read_text().splitlines()
    starts = [k for k, l in enumerate(lines) if l.startswith("G08 ")]
    last0 = lines[starts[0] + 7]
    lines[starts[0] + 7] = last0[:42].ljust(42) + " 0.000000000000E+00" * 2
    for k in starts[1:]:
        lines[k + 7] = lines[k + 7][:42].ljust(80)
    p = tmp_path / "blank_spare.rnx"
    p.write_text("\n".join(lines) + "\n")
    eph = ephemeris.parse_rinex(p)
    assert eph[8]["toe"] == pytest.approx(475200.0)      # the noon record survived
