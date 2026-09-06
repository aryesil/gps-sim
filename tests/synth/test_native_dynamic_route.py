"""SP-C: dynamic waypoint routes for the native engine.

With ``ScenarioRequest.route`` set, the native engine moves the receiver
along the route and re-evaluates every satellite's geometry per mixer
block, so acquisition near the end of the file matches the geometry at the
*moving* receiver -- including the receiver's own velocity in the Doppler.
"""
import datetime as dt
import json
import pathlib

from backend import config, geometry, inspector
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")
_START_LLH = (41.0082, 28.9784, 100.0)
_FS = 2_600_000.0
_DUR = 30.0
_T_TAIL = 29.5
# ~500 m/s due east for 30 s (Δlon ≈ 0.178° at this latitude).
_ROUTE = [(_START_LLH[0], _START_LLH[1], _START_LLH[2]),
          (_START_LLH[0], _START_LLH[1] + 0.178, _START_LLH[2])]


def _run(tmp_path, monkeypatch, route):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    start = dt.datetime(2021, 12, 11, 11, 59, 42)
    req = ScenarioRequest(
        rinex_path=_RINEX, lat=_START_LLH[0], lon=_START_LLH[1],
        alt=_START_LLH[2], start=start, duration_s=_DUR, sample_rate=_FS,
        sample_format="int16", engine="native", systems=("G",),
        nav_message=False, route=route)
    outdir = engine.run(req)
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    _week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(ephemeris.parse_rinex(_RINEX), _week, sow)
    return outdir, sow, eph


def _tail_doppler_err(outdir, sow, eph, rx_ecef, rx_vel):
    iq = inspector.read_iq(outdir / "gpssim.bin", "int16",
                           max_samples=int(_FS * 0.01),
                           offset_samples=int(_FS * _T_TAIL))
    sats = geometry.constellation(eph, rx_ecef, sow + _T_TAIL, mask_deg=10.0)
    truth = {e["prn"]: geometry.observables(eph[e["prn"]], rx_ecef,
                                            sow + _T_TAIL, rx_vel=rx_vel)
             for e in sats}
    cmp = {e["prn"]: e for e in inspector.compare(iq, _FS, sats)}
    out = {}
    for e in sats:
        prn = e["prn"]
        acq = inspector.acquire(iq, _FS, prn)
        if acq["metric_db"] < 12.0:
            continue
        out[prn] = (abs(acq["doppler_hz"] - truth[prn]["carrier_doppler_hz"]),
                    abs(cmp[prn]["code_phase_err_chips"]))
    return out


def test_route_tracks_the_moving_receiver(tmp_path, monkeypatch):
    outdir, sow, eph = _run(tmp_path, monkeypatch, _ROUTE)
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["route"] == {"waypoints": 2,
                                           "mode": "linear-interp"}
    rx_fn = engine._route_rx_fn(_ROUTE, _DUR)
    rx_end, v_end = rx_fn(_T_TAIL)
    errs = _tail_doppler_err(outdir, sow, eph, rx_end, v_end)
    assert len(errs) >= 4, errs
    for prn, (df, dc) in errs.items():
        assert df < 60.0, (prn, df)
        assert dc < 0.7, (prn, dc)


def test_static_run_ignores_receiver_velocity(tmp_path, monkeypatch):
    # A route run vs a static run at the same start point: judged against the
    # moving-receiver truth, the static IQ is off by the receiver-velocity
    # Doppler term (well past the moving path's tolerance for >=1 PRN).
    outdir, sow, eph = _run(tmp_path, monkeypatch, None)
    rx_fn = engine._route_rx_fn(_ROUTE, _DUR)
    rx_end, v_end = rx_fn(_T_TAIL)
    errs = _tail_doppler_err(outdir, sow, eph, rx_end, v_end)
    assert errs, errs
    assert max(df for df, _ in errs.values()) > 120.0, errs


def test_route_run_is_deterministic(tmp_path, monkeypatch):
    a, _s, _e = _run(tmp_path / "a", monkeypatch, _ROUTE)
    b, _s2, _e2 = _run(tmp_path / "b", monkeypatch, _ROUTE)
    assert (a / "gpssim.bin").read_bytes() == (b / "gpssim.bin").read_bytes()
