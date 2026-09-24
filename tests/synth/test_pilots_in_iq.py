"""Dataless pilot components ride with their data components in the native
IQ: L5 Q5 / E5a-Q / B2a-P in quadrature at equal power, Galileo E1-C next
to E1-B, and GPS L2C CM/CL time-multiplexed with CL on its 1.5 s epoch."""
import datetime as dt
import json
import math
import pathlib

import numpy as np
import pytest

from backend import config, geometry, inspector
from backend.analysis import band_acquire
from backend.ephem import ephemeris
from backend.scenario import ScenarioRequest
from backend.synth import _lib, engine

_FIX = pathlib.Path(__file__).parent.parent / "fixtures"
_RINEX = str(_FIX / "brdc_full.rnx")                 # GPS only
_START = dt.datetime(2021, 12, 11, 11, 59, 42)
_MIXED = str(_FIX / "brdc_mixed.rnx")                # G/E/C/..., 2026
_START_E = dt.datetime(2026, 9, 1, 6)
_START_C = dt.datetime(2026, 8, 30, 1, 29, 42)
_LLH = (41.0, 29.0, 100.0)


def _run(tmp_path, monkeypatch, systems, bands, fs, dur, *, rinex=_RINEX,
         start=_START, **kw):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = ScenarioRequest(
        rinex_path=rinex, lat=_LLH[0], lon=_LLH[1], alt=_LLH[2],
        start=start, duration_s=dur, sample_rate=fs, sample_format="int16",
        engine="native", systems=systems, bands=bands, **kw)
    out = engine.run(req)
    return out, json.loads((out / "meta.json").read_text())


def _corr(iq, fs, code, chip_hz, lag_chips, dopp_hz, n0=0, periods=1):
    """Complex correlation over ``periods`` code periods from sample n0 with
    the replica at the acquisition lag (band_acquire convention)."""
    L = code.size
    n = np.arange(n0, n0 + int(round(fs * periods * L / chip_hz)))
    idx = np.floor(n / fs * chip_hz - lag_chips).astype(np.int64) % L
    return complex(np.sum(iq[n] * code[idx]
                          * np.exp(-2j * np.pi * dopp_hz * n / fs)))


def _top(meta, sysc, band, k=2):
    svs = {s["prn"]: s["el_deg"] for s in meta["provenance"]["svs"]
           if s["sys"] == sysc and s["band"] == band}
    return sorted(svs, key=lambda p: -svs[p])[:k]


def _aligned(iq, fs, code, chip_hz, lag):
    """(n0, doppler): first code epoch after sample 0 (so a one-period
    window holds a single secondary-code chip) and the Doppler maximising
    the data correlation there."""
    n0 = int(math.ceil((lag % code.size) / chip_hz * fs)) + 1
    fr = np.arange(-6000.0, 6000.0, 25.0)
    pw = [abs(_corr(iq, fs, code, chip_hz, lag, f, n0=n0)) for f in fr]
    return n0, float(fr[int(np.argmax(pw))])


@pytest.mark.parametrize("sysc,rinex,start", [
    ("G", _RINEX, _START), ("E", _MIXED, _START_E), ("C", _MIXED, _START_C)])
def test_l5_band_pilots_are_in_quadrature_at_equal_power(
        tmp_path, monkeypatch, sysc, rinex, start):
    fs = 25_000_000.0
    out, meta = _run(tmp_path, monkeypatch, (sysc,), ["L5"], fs, 0.1,
                     rinex=rinex, start=start, l5_sample_rate=fs)
    assert not [w for w in meta["provenance"]["warnings"] if "pilot" in w]
    iq = inspector.read_iq(out / "gpssim_l5.bin", "int16",
                           max_samples=int(fs * 0.01))
    gen = {"G": _lib.code_l5, "E": _lib.code_e5a, "C": _lib.code_b2a}[sysc]
    prns = _top(meta, sysc, "L5")
    assert prns                    # the fixture may carry only one SV
    for prn in prns:
        data, pilot = (c.astype(np.float64) for c in gen(prn))
        a = band_acquire.acquire(iq, fs, data, chip_hz=10.23e6,
                                 code_len=10230, dopp_step=100.0, nperiods=1)
        assert a["metric_db"] > 10.0, (sysc, prn, a)
        lag = a["code_phase_chips"]
        n0, f = _aligned(iq, fs, data, 10.23e6, lag)
        ci = _corr(iq, fs, data, 10.23e6, lag, f, n0=n0)
        cq = _corr(iq, fs, pilot, 10.23e6, lag, f, n0=n0)
        assert 0.7 < abs(cq) / abs(ci) < 1.4, (sysc, prn, ci, cq)
        dphi = math.degrees(np.angle(cq / ci)) % 180.0
        assert abs(dphi - 90.0) < 20.0, (sysc, prn, dphi)


def test_e1_carries_e1c_pilot_with_e1b_data(tmp_path, monkeypatch):
    fs = 8_184_000.0
    out, meta = _run(tmp_path, monkeypatch, ("E",), None, fs, 0.05,
                     rinex=_MIXED, start=_START_E)
    assert meta["provenance"]["nav"].get("E/L1")
    iq = inspector.read_iq(out / meta["output"], "int16",
                           max_samples=int(fs * 0.02))
    checked = 0
    for prn in _top(meta, "E", "L1"):
        b = _lib.code(5, prn, 4092, 0)[0].astype(np.float64)
        c = _lib.code(6, prn, 4092, 25)[0].astype(np.float64)
        # BOC(1,1): two half-chips per chip -> replica at 2x the chip rate
        boc = np.tile([1.0, -1.0], 4092)
        b2, c2 = np.repeat(b, 2) * boc, np.repeat(c, 2) * boc
        a = band_acquire.acquire(iq, fs, c2, chip_hz=2.046e6, code_len=8184,
                                 dopp_step=100.0, nperiods=1)
        assert a["metric_db"] > 10.0, (prn, a)
        lag = a["code_phase_chips"]
        n0, f = _aligned(iq, fs, c2, 2.046e6, lag)
        cb = _corr(iq, fs, b2, 2.046e6, lag, f, n0=n0)
        cc = _corr(iq, fs, c2, 2.046e6, lag, f, n0=n0)
        assert 0.7 < abs(cb) / abs(cc) < 1.4, (prn, cb, cc)
        # (B - C)/sqrt(2) on one carrier: in phase up to the data/CS25 sign
        dphi = math.degrees(np.angle(cb / cc)) % 180.0
        assert min(dphi, 180.0 - dphi) < 20.0, (prn, dphi)
        checked += 1
    assert checked >= 1


def test_l2c_cl_sits_on_its_transmit_time_epoch(tmp_path, monkeypatch):
    fs = 4_092_000.0
    out, meta = _run(tmp_path, monkeypatch, ("G",), ["L2"], fs, 0.1,
                     nav_message=False)
    band, = meta["bands"]
    fs = float(band["fs"])                 # req.sample_rate governs L1 only
    iq = inspector.read_iq(out / band["file"], "int16",
                           max_samples=int(fs * 0.08))
    gps_start = _START + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(gps_start)
    eph = ephemeris.align_epochs(
        ephemeris.parse_rinex_multi(_RINEX, ("G",), at_gps=gps_start),
        week, sow, kepler_grid_s=engine._TOE_GRID_S,
        keep_real_within_s=ephemeris.REAL_EPH_WINDOW_S)
    rx = geometry.llh_to_ecef(*_LLH)
    pr = {e["prn"]: e["pseudorange_m"]
          for e in geometry.constellation(eph, rx, sow, mask_deg=5.0)}
    checked = 0
    for prn in _top(meta, "G", "L2"):
        a = band_acquire.acquire_l2c(iq, fs, prn)
        assert a["metric_db"] > 10.0, (prn, a)
        lag = 2.0 * a["code_phase_chips"]                 # TDM slots
        # first CM epoch in the capture: slot index 0
        n0, f = _aligned(iq, fs, band_acquire.l2c_cm_replica(prn), 1.023e6,
                         lag)
        _cm, cl = _lib.code_l2c(prn)
        cm_pow = abs(_corr(iq, fs, band_acquire.l2c_cm_replica(prn), 1.023e6,
                           lag, f, n0=n0))
        best = []
        for k in range(75):
            rep = np.zeros(20460)
            rep[1::2] = cl[k * 10230:(k + 1) * 10230]
            best.append(abs(_corr(iq, fs, rep, 1.023e6, lag, f, n0=n0)))
        k_got = int(np.argmax(best))
        assert best[k_got] > 0.7 * cm_pow, (prn, best[k_got], cm_pow)
        t_tx = sow + n0 / fs - pr[prn] / config.C
        k_exp = int(round((t_tx % 1.5) / 0.02)) % 75
        assert k_got == k_exp, (prn, k_got, k_exp)
        checked += 1
    assert checked >= 1


def test_e1_is_cboc_at_wide_sample_rates(tmp_path, monkeypatch):
    """At fs >= 14 MHz E1 carries CBOC(6,1,1/11): the sc(6,1) share is
    beta/alpha = 1/sqrt(10) of the sc(1,1) share, + on E1-B and - on E1-C."""
    fs = 16_368_000.0
    out, meta = _run(tmp_path, monkeypatch, ("E",), None, fs, 0.03,
                     rinex=_MIXED, start=_START_E)
    iq = inspector.read_iq(out / meta["output"], "int16",
                           max_samples=int(fs * 0.02))
    sc11 = np.tile(np.repeat([1.0, -1.0], 6), 4092)
    sc61 = np.tile([1.0, -1.0], 6 * 4092)
    checked = 0
    for prn in _top(meta, "E", "L1"):
        b = _lib.code(5, prn, 4092, 0)[0].astype(np.float64)
        c = _lib.code(6, prn, 4092, 25)[0].astype(np.float64)
        c2 = np.repeat(c, 2) * np.tile([1.0, -1.0], 4092)
        a = band_acquire.acquire(iq, fs, c2, chip_hz=2.046e6, code_len=8184,
                                 dopp_step=100.0, nperiods=1)
        assert a["metric_db"] > 10.0, (prn, a)
        n0, f = _aligned(iq, fs, c2, 2.046e6, a["code_phase_chips"])
        lag0 = 6.0 * a["code_phase_chips"]         # 12.276 MHz sub-chips
        for code, sign in ((b, 1.0), (c, -1.0)):
            p = np.repeat(code, 12)
            # acquisition resolves ~0.75 sub-chip; the sc(6,1) peak is one
            # sub-chip wide, so refine the lag on the matched CBOC replica
            match = p * (sc11 + sign * sc61 / math.sqrt(10.0))
            lag = max(np.arange(lag0 - 2.0, lag0 + 2.0, 0.1), key=lambda g: abs(
                _corr(iq, fs, match, 12.276e6, g, f, n0=n0)))
            r11 = _corr(iq, fs, p * sc11, 12.276e6, lag, f, n0=n0)
            r61 = _corr(iq, fs, p * sc61, 12.276e6, lag, f, n0=n0)
            ratio = (r61 / r11).real
            assert abs(ratio - sign / math.sqrt(10.0)) < 0.1, (prn, sign, ratio)
        checked += 1
    assert checked >= 1
