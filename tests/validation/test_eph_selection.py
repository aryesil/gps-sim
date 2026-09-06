"""Deterministic broadcast-ephemeris record selection (backend.ephem.eph_select)."""
import pathlib

import pytest

from backend.ephem import eph_select
from backend.ephem.eph_select import RecordRef, select

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "brdc_sample.rnx"
WEEK = 2433
BASE = WEEK * 604800.0 + 475200.0     # continuous GPS seconds of the fixture toe


def _rec(prn, toe_off, *, iode=10, iodc=10, health=0):
    toe = BASE + toe_off
    return RecordRef(prn=prn, toe_gps_s=toe, toc_gps_s=toe, gps_week=WEEK,
                     iode=iode, iodc=iodc, health=health,
                     eph={"toe": 475200.0 + toe_off, "toc": 475200.0 + toe_off,
                          "gps_week": WEEK, "prn": prn})


def test_picks_newest_record_at_or_before_epoch():
    recs = {1: [_rec(1, -7200, iode=1), _rec(1, -1800, iode=2), _rec(1, +5400, iode=3)]}
    rep = select(recs, BASE)
    assert rep.selected[1].iode == 2          # -1800 s is newest at/before the epoch
    assert len(rep.rejected[1]) == 2          # the other two are named as rejected


def test_future_only_records_use_closest_future_within_lead():
    recs = {1: [_rec(1, +1200, iode=5), _rec(1, +6000, iode=6)]}
    rep = select(recs, BASE)
    assert rep.selected[1].iode == 5


def test_record_too_far_in_future_is_rejected():
    recs = {1: [_rec(1, +9000, iode=7)]}
    rep = select(recs, BASE)
    assert 1 not in rep.selected
    assert any("future" in reason for _, reason in rep.rejected[1])


def test_stale_record_rejected_by_default_used_when_allowed():
    recs = {1: [_rec(1, -9000, iode=8)]}
    rep = select(recs, BASE)
    assert 1 not in rep.selected
    assert any("stale" in reason for _, reason in rep.rejected[1])

    rep2 = select(recs, BASE, allow_stale=True)
    assert rep2.selected[1].iode == 8
    assert rep2.stale_used == [1]


def test_unhealthy_record_dropped_and_reported():
    recs = {1: [_rec(1, -600, health=63)]}
    rep = select(recs, BASE)
    assert 1 not in rep.selected
    assert rep.unhealthy == [1]

    rep2 = select(recs, BASE, require_healthy=False)
    assert rep2.selected[1].health == 63


def test_unhealthy_ignored_when_a_healthy_record_exists():
    recs = {1: [_rec(1, -600, iode=1, health=63), _rec(1, -1200, iode=2, health=0)]}
    rep = select(recs, BASE)
    assert rep.selected[1].iode == 2
    assert rep.unhealthy == []


def test_missing_prn_is_listed_not_raised():
    rep = select({1: [_rec(1, -600)]}, BASE, prns=[1, 17])
    assert 1 in rep.selected
    assert rep.missing == [17]


def test_selection_is_deterministic_under_tie():
    a = [_rec(1, 0, iode=1, iodc=5), _rec(1, 0, iode=1, iodc=9)]
    r1 = select({1: list(a)}, BASE).selected[1]
    r2 = select({1: list(reversed(a))}, BASE).selected[1]
    assert r1.iodc == r2.iodc == 9          # higher iodc wins, order-independent


def test_identifier_is_json_safe_and_names_the_choice():
    import json
    rep = select({1: [_rec(1, -1800, iode=4)], 2: [_rec(2, -600, iode=5)]}, BASE)
    ident = rep.identifier()
    json.dumps(ident)                        # must not raise
    assert ident["selected"][1]["iode"] == 4
    assert ident["selected"][1]["age_s"] == pytest.approx(1800.0, abs=1.0)


def test_parse_rinex_records_reads_the_real_fixture():
    recs = eph_select.parse_rinex_records(FIX)
    assert set(recs) == set(range(1, 11))
    for prn, lst in recs.items():
        assert len(lst) >= 1
        assert all(isinstance(r, RecordRef) for r in lst)
        assert all(r.gps_week > 2000 for r in lst)
    # and the selector runs end to end on it
    rep = select(recs, BASE, allow_stale=True)
    assert len(rep.selected) == 10
