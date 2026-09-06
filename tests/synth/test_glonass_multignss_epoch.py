"""GLONASS lines up with the rest of the constellation on the one GPS-SoW
run-start epoch: align_epochs rewrites every R/S ``toe_ref`` to ``sow`` and the
PZ-90 integrator propagates on ``t_gps - toe_ref``, so a GLONASS SV evaluated by
``constellation_multi`` at ``sow`` must be self-consistent."""
import datetime as dt
import pathlib

from backend import config, ephemeris, geometry
from backend.synth import signals

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")


def test_glonass_position_self_consistent_at_run_start_epoch():
    start = dt.datetime(2026, 9, 1)
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.parse_rinex_multi(_MIXED, ("G", "R"))
    eph = ephemeris.align_epochs(eph, week, sow)

    # align_epochs must handle tuple (sys, prn) keys, not just bare-int GPS.
    for key in eph:
        if isinstance(key, tuple) and key[0] == "R":
            assert eph[key]["toe_ref"] == sow

    rx = geometry.llh_to_ecef(41.0, 29.0, 100.0)
    entries = geometry.constellation_multi(eph, rx, sow, signals.signal_for,
                                           mask_deg=-90.0)  # keep all, incl. below horizon
    glo = [e for e in entries if e["sys"] == "R"]
    assert glo, "fixture exposes no GLONASS SVs"
    for e in glo:
        assert e["glo_k"] is not None
        assert -90.0 <= e["el_deg"] <= 90.0
        assert 0.0 <= e["az_deg"] <= 360.0
        assert 1.9e7 <= e["geo_range_m"] <= 2.7e7, e["geo_range_m"]
        assert abs(e["carrier_doppler_hz"]) < 5000.0, e["carrier_doppler_hz"]
