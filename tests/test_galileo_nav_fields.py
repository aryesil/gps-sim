"""Galileo I/NAV fields a real receiver needs before it uses a satellite in
a fix: GST week (GPS week - 1024), the RINEX record nearest the run, and
the Galileo-named RINEX fields (IODnav, BGD, SISA)."""
import datetime as dt
import pathlib

import georinex as gr
import numpy as np

from backend import config
from backend.analysis import inav_encode, nav_encoders
from backend.ephem import ephemeris
from backend.synth import signals

_RINEX = str(pathlib.Path(__file__).parent / "fixtures" / "brdc_mixed.rnx")
_START = dt.datetime(2026, 9, 1, 6)


def _eph():
    g = _START + dt.timedelta(seconds=config.GPS_UTC_LEAP_S)
    week, sow = ephemeris.gps_week_and_sow(g)
    return ephemeris.parse_rinex_multi(_RINEX, ("E",), at_gps=g), week, sow


def _words(arr):
    syms = [1 if s > 0 else 0 for s in arr]
    out = {}
    for p in range(len(syms) // 500):
        wtype, word = inav_encode.decode_word(syms[p * 500:(p + 1) * 500])
        out.setdefault(wtype, word)
    return out


def _u(bits):
    return int("".join(str(b) for b in bits), 2)


def test_inav_broadcasts_gst_week_and_valid_time():
    eph, week, sow = _eph()
    rec = next(v for k, v in eph.items() if k[0] == "E")
    arr, _rate = nav_encoders.nav_stream_for(
        "E", signals.SIGNALS["GAL_E1"], rec, {}, week, sow, 14.0)
    w = _words(arr)
    # word type 5: 6+11+11+14+5+10+10+2+2+1+1 = 73 bits before WN
    assert _u(w[5][73:85]) == week - 1024
    # word type 0: Time field '10' marks WN/TOW valid; WN at bit 96
    assert w[0][6:8] == [1, 0]
    assert _u(w[0][96:108]) == week - 1024


def test_galileo_record_is_nearest_the_run_and_fields_are_mapped():
    eph, week, sow = _eph()
    nav = gr.load(_RINEX, use="E")
    for (s, prn), rec in eph.items():
        sub = nav.sel(sv=f"E{prn:02d}").dropna(dim="time", how="all")
        toes = (sub["GALWeek"].values - week) * 604800.0 + sub["Toe"].values
        toes = toes[np.isfinite(toes)]
        best = toes[np.argmin(np.abs(toes - sow))]
        assert abs(rec["toe"] - best) < 1.0, (prn, rec["toe"], best)
        for k in ("iode", "tgd", "tgd_e5a", "sisa"):
            assert k in rec, (prn, k)
        assert inav_encode.sisa_index(rec["sisa"]) != 255


def test_sisa_index_follows_icd_table():
    assert inav_encode.sisa_index(0.30) == 30
    assert inav_encode.sisa_index(3.12) == 107
    assert inav_encode.sisa_index(float("nan")) == 107


def _records(week_var, week, toes):
    import xarray as xr
    t = np.arange(len(toes)).astype("datetime64[h]")
    return xr.Dataset({week_var: ("time", np.full(len(toes), float(week))),
                       "Toe": ("time", np.asarray(toes, float))},
                      coords={"time": t})


def test_pick_epoch_reads_the_galileo_and_beidou_week_fields():
    week = 2434
    target = week * 604800.0 + 179_058.0
    # Galileo: GALWeek aligned to GPS; the nearest record must win, not the
    # last one of the day.
    sub = _records("GALWeek", week, [172_800.0, 180_000.0, 257_400.0])
    assert float(ephemeris._pick_epoch(sub, target, "E")["Toe"]) == 180_000.0
    # BeiDou: BDTWeek = GPS week - 1356, toe on BDT (GPS - 14 s)
    sub = _records("BDTWeek", week - 1356, [172_800.0, 176_400.0, 255_600.0])
    assert float(ephemeris._pick_epoch(sub, target, "C")["Toe"]) == 176_400.0
