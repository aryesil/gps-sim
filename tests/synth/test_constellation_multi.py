import datetime as dt
import pathlib

from backend import config, ephemeris, geometry
from backend.synth import engine, signals
from backend.scenario import ScenarioRequest

_GPS2 = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def _req():
    return ScenarioRequest(rinex_path=_GPS2, lat=41.0, lon=29.0, alt=100.0,
                           start=dt.datetime(2024, 1, 1), duration_s=5,
                           sample_rate=2_600_000.0, sample_format="int16")


def test_constellation_multi_gps_only_matches_visible_gps():
    req = _req()
    ref = engine._visible_gps(req)                    # Phase 1 path
    week, sow = ephemeris.gps_week_and_sow(
        req.start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S))
    eph = ephemeris.align_epochs(
        ephemeris.parse_rinex_multi(_GPS2, ("G",)), week, sow)
    rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
    got = geometry.constellation_multi(
        {("G", p): v for p, v in eph.items()}, rx, sow,
        signals.signal_for, mask_deg=5.0)
    ref_prns = sorted(p for p, _ in ref)
    got_prns = sorted(e["prn"] for e in got if e["sys"] == "G")
    assert got_prns == ref_prns
    by_prn = {e["prn"]: e for e in got}
    for p, o in ref:
        assert abs(by_prn[p]["code_phase_chips"] - o["code_phase_chips"]) < 1e-6
        assert abs(by_prn[p]["carrier_doppler_hz"] - o["carrier_doppler_hz"]) < 1e-6


def test_constellation_multi_uses_state_fn_override():
    rx = geometry.llh_to_ecef(41.0, 29.0, 100.0)
    called = {}

    def fake_fn(sow):
        called["hit"] = True
        return ([2.0e7, 0.0, 1.5e7], [0.0, 0.0, 0.0], 0.0)

    ents = geometry.constellation_multi(
        {("E", 11): {"system": "E"}}, rx, 0.0,
        signals.signal_for,
        mask_deg=0.0, state_fn_by_key={("E", 11): fake_fn})
    assert called.get("hit") and ents and ents[0]["sys"] == "E"
