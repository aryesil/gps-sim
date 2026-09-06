"""SP-B: continuous per-block re-propagation of every satellite's state.

With ``continuous_doppler=True`` the code phase, code Doppler and carrier
Doppler evolve over the run, so a receiver still finds each satellite at
the geometrically correct place near the END of a long capture. With the
flag off, the run-start Doppler is held constant and the same end-of-file
acquisition drifts away from the truth.
"""
import datetime as dt
import pathlib

from backend import config, geometry, inspector
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import engine

_RINEX = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")
_RX = (41.0082, 28.9784, 100.0)
_FS = 2_600_000.0
_DUR = 45.0
_T_TAIL = 44.5          # acquire a slice starting here (near end of file)


def _run(tmp_path, monkeypatch, continuous):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    start = dt.datetime(2021, 12, 11, 11, 59, 42)
    req = ScenarioRequest(
        rinex_path=_RINEX, lat=_RX[0], lon=_RX[1], alt=_RX[2], start=start,
        duration_s=_DUR, sample_rate=_FS, sample_format="int16",
        engine="native", systems=("G",), nav_message=False)
    req.continuous_doppler = continuous
    outdir = engine.run(req)
    gps_start = start + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    _week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(ephemeris.parse_rinex(_RINEX), _week, sow)
    return outdir, sow, eph


def _tail_errors(outdir, sow, eph):
    """Per-PRN (doppler_err_hz, code_phase_err_chips) from a 10 ms slice
    taken _T_TAIL seconds into the file, vs geometry at sow + _T_TAIL."""
    iq = inspector.read_iq(outdir / "gpssim.bin", "int16",
                           max_samples=int(_FS * 0.01),
                           offset_samples=int(_FS * _T_TAIL))
    rx = geometry.llh_to_ecef(*_RX)
    sats = geometry.constellation(eph, rx, sow + _T_TAIL, mask_deg=10.0)
    cmp = {e["prn"]: e for e in inspector.compare(iq, _FS, sats)}
    out = {}
    for e in sats:
        prn = e["prn"]
        acq = inspector.acquire(iq, _FS, prn)
        if acq["metric_db"] < 12.0:
            continue
        out[prn] = (abs(acq["doppler_hz"] - e["carrier_doppler_hz"]),
                    abs(cmp[prn]["code_phase_err_chips"]))
    return out


def test_continuous_doppler_removes_end_of_run_drift(tmp_path, monkeypatch):
    outdir, sow, eph = _run(tmp_path, monkeypatch, continuous=True)
    errs = _tail_errors(outdir, sow, eph)
    assert len(errs) >= 4, errs
    for prn, (df, dc) in errs.items():
        assert df < 60.0, (prn, df)
        assert dc < 0.7, (prn, dc)


def test_flag_off_drifts_more_than_flag_on(tmp_path, monkeypatch):
    # Guards against the knots silently not being used: over a 45 s run the
    # constant-Doppler path accumulates a materially larger end-of-file code
    # phase error than the per-block re-propagation path.
    on_dir, sow, eph = _run(tmp_path / "on", monkeypatch, continuous=True)
    off_dir, _s, _e = _run(tmp_path / "off", monkeypatch, continuous=False)
    on = _tail_errors(on_dir, sow, eph)
    off = _tail_errors(off_dir, sow, eph)
    common = sorted(set(on) & set(off))
    assert len(common) >= 4, (on, off)
    worse = [p for p in common if off[p][1] > on[p][1] + 0.1]
    assert len(worse) >= max(1, len(common) // 2), (on, off)


def test_continuous_doppler_is_deterministic(tmp_path, monkeypatch):
    a, _s, _e = _run(tmp_path / "a", monkeypatch, continuous=True)
    b, _s2, _e2 = _run(tmp_path / "b", monkeypatch, continuous=True)
    assert (a / "gpssim.bin").read_bytes() == (b / "gpssim.bin").read_bytes()
