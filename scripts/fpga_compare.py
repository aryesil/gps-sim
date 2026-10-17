"""C++ mixer vs FPGA host-model comparison harness.

Usage:  PYTHONPATH=. python scripts/fpga_compare.py --systems GCE --fs 30e6 --blocks 2

Composite: the whole band runs through the C++ mixer in memory (quant=2, int16)
and through generate_blocks(out_lsb=32767) -> EngineModel, block by block.

Per channel: each channel is run alone.  The C++ scale of a one-element band is
0.25*32767 (nsv = 1) while the model/composite scale is
0.25*out_lsb/sqrt(nsv_total).  To test a channel at the amplitude it has inside
the composite, the model is generated with out_lsb = 32767/sqrt(nsv_total) and
the (int16, finer-grained) C++ output is multiplied by 1/sqrt(nsv_total).
"""
from __future__ import annotations

import argparse
import ctypes  # noqa: F401
import datetime as dt
import math
import pathlib
import types

import numpy as np

from backend.fpga.cmdgen import generate_blocks
from backend.fpga.model import EngineModel
from backend.scenario import ScenarioRequest
from backend.synth import _lib, engine

B = 65536
DEFAULT_RINEX = pathlib.Path(__file__).parents[1] / "data" / "rinex" / "BRDC_2026289.rnx"


def make_req(systems=("G", "C", "E"), fs=30e6, n_blocks=2, rinex=DEFAULT_RINEX,
             start=dt.datetime(2026, 10, 16, 12)):
    return ScenarioRequest(
        rinex_path=str(rinex), lat=41.0, lon=29.0, alt=100.0, start=start,
        in_memory=True, duration_s=n_blocks * B / fs, sample_rate=fs,
        sample_format="int16", systems=tuple(systems))


def _run_cpp(svs, fs, total):
    bs = _lib.BandSpec()
    out = np.empty(total * 2, np.int16)
    _lib.fill_band(bs, None, fs, 2, total, svs, block_samples=B, out_mem=out)
    assert _lib.run_bands([bs]) == 0
    return out[0::2].astype(np.float64) + 1j * out[1::2].astype(np.float64)


def _band(svs, fs, n_blocks, owner):
    return types.SimpleNamespace(fs=fs, block_samples=B, total_samples=n_blocks * B,
                                 svs=list(svs), keep_alive=owner.keep_alive)


def _fit_out_lsb(band, out_lsb):
    """Largest level <= out_lsb whose coefficients fit COEF_BITS (nsv small
    and out_lsb = 32767 overflows the 18-bit coefficient)."""
    while True:
        try:
            generate_blocks(band, n_blocks=0, out_lsb=out_lsb)
            return out_lsb
        except ValueError as e:
            if "does not fit" not in str(e):
                raise
            out_lsb *= 0.97


def _run_model(band, n_blocks, out_lsb):
    m = EngineModel(band.fs)
    ys = []
    for j, recs in generate_blocks(band, out_lsb=out_lsb):
        ys.append(m.feed_block(j, recs))
    y = np.concatenate(ys).astype(np.float64)
    return y[:, 0] + 1j * y[:, 1], m


def _rms(x):
    return float(np.sqrt(np.mean(np.abs(x) ** 2)))


def _db(x):
    return 20 * math.log10(max(x, 1e-30))


def _subset(svs):
    """2 GPS, 2 Galileo (one E1C, one E1B), 2 BeiDou: channel indices."""
    def pick(pred, n):
        return [i for i, s in enumerate(svs) if pred(s)][:n]
    g = pick(lambda s: s.sys == 0, 2)
    c = pick(lambda s: s.sys == 3, 2)
    e = pick(lambda s: s.sys == 5 and s.nav_mode == 0, 1) + \
        pick(lambda s: s.sys == 5 and s.nav_mode != 0, 1)
    return g + e + c


def compare(req, n_blocks: int, per_channel: bool | None = True,
            out_lsb: float | None = None) -> dict:
    """out_lsb: model output level (C++ is scaled by out_lsb/32767 for the
    comparison).  None = 32767, lowered in 3% steps only if the coefficients do
    not fit COEF_BITS (reported as res['out_lsb'] / res['level_reduced']).
    per_channel: False = none; None = all channels if <= 12 else skipped;
    True = all channels if <= 12 else the representative 6-channel subset."""
    plan = engine.plan(req)
    band = plan.bands[0]
    fs = band.fs
    nsv = len(band.svs)
    nb = min(n_blocks, band.total_samples // B)
    total = nb * B
    full = _band(band.svs, fs, nb, band)
    lvl = 32767.0 if out_lsb is None else float(out_lsb)
    want = lvl
    if out_lsb is None:
        lvl = _fit_out_lsb(full, lvl)
    lf = lvl / 32767.0
    cpp = _run_cpp(band.svs, fs, total) * lf
    mod, m = _run_model(full, nb, lvl)
    d = mod - cpp
    res = dict(
        fs=fs, nsv=nsv, n_blocks=nb, out_lsb=lvl, level_reduced=lvl < want,
        nrmse_db=_db(_rms(d) / _rms(cpp)),
        clipped=m.clipped, underruns=m.underruns,
        peak_cpp=float(max(np.abs(cpp.real).max(), np.abs(cpp.imag).max())),
        peak_model=float(max(np.abs(mod.real).max(), np.abs(mod.imag).max())),
        frac_gt2lsb=float(np.mean((np.abs(d.real) > 2) | (np.abs(d.imag) > 2))),
        err_rms_lsb=_rms(d),
        err_max_lsb=float(max(np.abs(d.real).max(), np.abs(d.imag).max())),
        err_exact_frac=float(np.mean(d == 0)),
        err_pct=[float(np.percentile(np.abs(np.concatenate([d.real, d.imag])), p))
                 for p in (50, 90, 99, 99.9)],
        channels=[])
    if per_channel is False or (per_channel is None and nsv > 12):
        return res
    idx = list(range(nsv)) if nsv <= 12 else _subset(band.svs)
    fac = lf / math.sqrt(nsv)
    for i in idx:
        sv = band.svs[i]
        sub = _band([sv], fs, nb, band)
        ref = _run_cpp([sv], fs, total) * fac
        y, mm = _run_model(sub, nb, 32767.0 * fac)
        ok = np.abs(ref) > 0
        cross = np.sum(ref * np.conj(y))
        ph = math.degrees(np.angle(cross))
        gain = _db(_rms(y) / _rms(ref))
        dd = np.abs(y - ref)
        peak = max(np.abs(ref.real).max(), np.abs(ref.imag).max())
        edge = float(np.mean(dd > 0.02 * peak))
        res["channels"].append(dict(
            idx=i, sys=int(sv.sys), prn=int(sv.prn), nav=int(sv.nav_mode != 0),
            sec_len=int(sv.sec_len), boc=int(sv.sub_carrier_hz > 0),
            phase_err_deg=ph, gain_err_db=gain, edge_mismatch_frac=edge,
            nrmse_db=_db(_rms(y - ref) / _rms(ref)), clipped=mm.clipped,
            peak=float(peak)))
    return res


def print_table(res):
    print(f"fs={res['fs']/1e6:g} Msps  channels={res['nsv']}  blocks={res['n_blocks']}")
    print(f"out_lsb = {res['out_lsb']:.0f}" +
          ("  (REDUCED: 18-bit coefficient overflow at 32767)" if res["level_reduced"] else ""))
    print(f"composite nrmse = {res['nrmse_db']:.2f} dB   clipped = {res['clipped']}   "
          f"underruns = {res['underruns']}")
    print(f"peak cpp/model = {res['peak_cpp']:.0f}/{res['peak_model']:.0f}   "
          f"|err|>2LSB = {res['frac_gt2lsb']:.2e}   err rms = {res['err_rms_lsb']:.3f} LSB   "
          f"max = {res['err_max_lsb']:.0f}   exact = {res['err_exact_frac']:.3f}")
    print("|err| percentiles 50/90/99/99.9 = " +
          "/".join(f"{v:.1f}" for v in res["err_pct"]))
    if res["channels"]:
        print(f"{'ch':>3} {'sys':>3} {'prn':>3} {'nav':>3} {'sec':>3} {'boc':>3} "
              f"{'phase[deg]':>11} {'gain[dB]':>9} {'edge':>9} {'nrmse[dB]':>10} {'clip':>5}")
        for c in res["channels"]:
            print(f"{c['idx']:>3} {c['sys']:>3} {c['prn']:>3} {c['nav']:>3} "
                  f"{c['sec_len']:>3} {c['boc']:>3} {c['phase_err_deg']:>11.4f} "
                  f"{c['gain_err_db']:>9.4f} {c['edge_mismatch_frac']:>9.2e} "
                  f"{c['nrmse_db']:>10.2f} {c['clipped']:>5}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", default="GCE")
    ap.add_argument("--fs", type=float, default=30e6)
    ap.add_argument("--blocks", type=int, default=2)
    ap.add_argument("--rinex", default=str(DEFAULT_RINEX))
    ap.add_argument("--no-per-channel", action="store_true")
    a = ap.parse_args()
    req = make_req(tuple(a.systems), a.fs, a.blocks, a.rinex)
    print_table(compare(req, a.blocks, per_channel=False if a.no_per_channel else True))


if __name__ == "__main__":
    main()
