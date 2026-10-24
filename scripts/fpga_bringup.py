#!/usr/bin/env python
"""Bring-up test of the host path to the FPGA GNSS engine (backend/fpga/tx.py).

Two modes (all runs keep TX gain at its minimum, no RF of any use):

  loopback (default)  AD9361 digital loopback in FDD, only <= 10 Msps. For each
        fs in --fs (default 5e6 10e6) and each satellite count in --nsv
        (default 1 11): run the engine for --duration s (default 8), capture RX
        buffers, align on the bit-exact model (FFT cross-correlation), report
        NRMSE after a least-squares complex gain, per-channel correlation and
        the gs_wrap counters. loopback=1 is restored to 0 afterwards.
        PASS: NRMSE < -30 dB, all counters 0, no host-lead warning.

  --tdd-counters      TDD mode (config.TX_TDD_MODE=True), any fs (default 20e6
        30e6), no RX: run --duration s (default 60) and poll the counters,
        play index lag and temperature. PASS: counters 0, no lead warning.

Safety: FDD above 10 Msps is refused; the run aborts if the XADC or AD9361
temperature exceeds --max-temp (70 C); cleanup (always): engine disable,
mute, soft_reset, cmd_mode off, loopback 0, AD9361 ensm_mode 'alert'.

Board access is ssh (root@host from --uri) for the gs_wrap registers
(`devmem`, one round trip per poll) and sysfs temperature / ensm reads, plus
libiio (--uri) for the radio. `--fake` runs everything against an in-memory
engine and radio (no network), for checking the script itself.

Examples
  python scripts/fpga_bringup.py --fake
  python scripts/fpga_bringup.py --uri ip:192.168.3.1
  python scripts/fpga_bringup.py --tdd-counters --fs 20e6 30e6 --duration 60
Result table on stdout, JSON in --json (default scripts/fpga_bringup_result.json).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import math
import pathlib
import queue
import sys
import threading
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import config                                      # noqa: E402
from backend.fpga import cmdfmt, roms                           # noqa: E402
from backend.fpga import device as D                            # noqa: E402
from backend.fpga import widths as W                            # noqa: E402
from backend.fpga.cmdgen import generate_blocks                 # noqa: E402
from backend.fpga.model import EngineModel                      # noqa: E402
from backend.fpga.stream import CommandStream                   # noqa: E402
from backend.fpga.tx import GnssTx                              # noqa: E402
from backend.synth import _lib                                  # noqa: E402
from backend.synth.engine import BandPlan                       # noqa: E402

B = W.BLOCK
MUTE_DB = -89.75
FDD_MAX_FS = 10e6
NRMSE_MAX_DB = -30.0
FAKE_SHIFT = 777                    # fake radio: samples of loopback delay

_SYSFS = "/sys/bus/iio/devices"
_BOARD_INFO = (
    "for d in " + _SYSFS + "/iio:device*; do n=$(cat $d/name); "
    "case $n in "
    "ad9361-phy) echo ensm $(cat $d/ensm_mode); echo adtemp $(cat $d/in_temp0_input);; "
    "xadc) echo xadc $(awk \"BEGIN{print ($(cat $d/in_temp0_raw)+$(cat $d/in_temp0_offset))"
    "*$(cat $d/in_temp0_scale)/1000}\");; "
    "esac; done")


# ----------------------------------------------------------------- band
def make_band(fs: float, nsv: int, total_blocks: int, prns=None) -> BandPlan:
    """nsv GPS C/A channels (PRN 1..nsv), distinct Doppler / phase / code phase."""
    svs, keep = [], []
    for i in range(nsv):
        prn = int(prns[i]) if prns else i + 1
        f = (1500.0 * (i + 1)) * (1 if i % 2 == 0 else -1)
        s = _lib.SvSpec()
        sysc = 0 if prn <= 32 else 1 if prn >= 193 else 2   # GPS / QZSS / SBAS L1 C/A family
        rom_prn = roms.ca_index(sysc, prn)
        code = np.ascontiguousarray(roms.code_chips(cmdfmt.KIND_CA, rom_prn), dtype=np.int8)
        keep.append(code)
        s.code = ctypes.cast(code.ctypes.data, ctypes.POINTER(ctypes.c_int8))
        s.code_len = 1023
        s.chip_rate_hz = 1.023e6
        s.sys = sysc
        s.prn = prn
        s.carrier_freq_hz = f
        s.carrier_phase0_rad = 0.37 * i
        s.code_phase0_chips = 41.0 * i
        s.code_doppler_hz = f / 1575.42e6 * 1.023e6
        s.gain = 1.0
        s.tx_time_valid = 1
        s.tx_chips_offset = 0.0
        s.sub_carrier_hz = 0.0
        s.cboc = 0
        s._keep = keep[-1:]
        svs.append(s)
    return BandPlan(id="L1", fs=fs, centre_hz=config.L1_HZ, quant=2,
                    total_samples=total_blocks * B, block_samples=B, svs=svs,
                    keep_alive=keep, nsv_scale=1.0,
                    sv_el_deg=[60.0 - i for i in range(nsv)])


class Prefetch:
    """Iterator that runs a (CPU heavy) generator in a thread, bounded queue."""

    def __init__(self, it, depth=64):
        self._q = queue.Queue(maxsize=depth)
        self.count = 0
        self.t0 = time.monotonic()
        self.error = None
        threading.Thread(target=self._run, args=(it,), daemon=True).start()

    def _run(self, it):
        try:
            for x in it:
                self._q.put(x)
        except Exception as ex:                      # noqa: BLE001
            self.error = ex
        self._q.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        x = self._q.get()
        if x is None:
            if self.error:
                raise self.error
            raise StopIteration
        self.count += x.n_blocks
        return x


# ----------------------------------------------------------------- radios
class FakeRadio:
    """In-memory radio: write_words feeds the FakeDevice; rx() replays the
    engine output >>4 (12-bit DAC) delayed by FAKE_SHIFT samples."""

    def __init__(self, dev: D.FakeDevice):
        self.dev, self._pos = dev, 0
        self.loopback = 0

    def open(self): pass
    def write_words(self, w): self.dev.push_words(w.tobytes())
    def temperature_c(self): return 41.0

    def rx(self, n):
        end = time.monotonic() + 20.0
        while time.monotonic() < end:           # a real DMA buffer is only handed
            s = self.dev.engine.samples         # over once it is full
            if s and sum(len(x) for x in s) + FAKE_SHIFT >= self._pos + n:
                break
            time.sleep(0.02)
        if not s:
            return np.zeros(n, np.complex64)
        a = np.concatenate(s)
        a = np.concatenate([np.zeros((FAKE_SHIFT, 2), np.int16), a])
        seg = a[self._pos:self._pos + n]
        self._pos += n
        out = np.zeros(n, np.complex64)
        out[:len(seg)] = (seg[:, 0] + 1j * seg[:, 1]) / 16.0
        return out

    def close(self): pass


class RealRadio:
    def __init__(self, uri, fs, rx, rx_lo_on):
        from backend.rf.backends import BACKENDS
        self.be, self.uri, self.fs, self.rx_on, self.rx_lo_on = (
            BACKENDS["tezuka_gnss"], uri, fs, rx, rx_lo_on)
        self.h = None
        self.rx_n = 1 << 17

    def open(self):
        self._was_tdd = False
        if self.rx_on:
            # loopback needs FDD; a board that boots in TDD is switched for the
            # test (before the TX setup, the switch re-initialises the chip) and
            # put back afterwards
            from backend.rf.backends import ad9361
            probe = ad9361.probe_open(self.uri)
            self._was_tdd = ad9361._chip_is_tdd(probe)
            if self._was_tdd:
                ad9361._set_duplex_mode(probe, fdd=True)
            del probe
        self.h = self.be.open_tx(self.uri, config.L1_HZ, self.fs)
        self.be.set_gain(self.h, 0, MUTE_DB)             # minimum, never raised
        if self.rx_on:
            from backend.rf.backends import ad9361
            if self.rx_lo_on:
                ad9361._set_rx_lo_powerdown(self.h, False)
            self.h.loopback = 1                          # digital BIST TX -> RX
            self.h.rx_enabled_channels = [0]
            self.h.rx_buffer_size = self.rx_n

    def write_words(self, w): self.be.write_words(self.h, w)
    def temperature_c(self): return self.be.temperature_c(self.h)

    def rx(self, n):
        return np.asarray(self.h.rx(), dtype=np.complex64)

    def close(self):
        """Mute, loopback 0, release RX, LOs off, ensm alert."""
        h, self.h = self.h, None
        if h is None:
            return
        for step in (lambda: setattr(h, "loopback", 0),
                     lambda: h.rx_destroy_buffer(),
                     lambda: self.be.set_gain(h, 0, MUTE_DB),
                     lambda: self.be.close(h)):    # mute, buffer, LOs, ensm alert
            try:
                step()
            except Exception as ex:                       # noqa: BLE001
                print(f"  cleanup step failed: {ex}")
        try:
            from backend.rf.backends import ad9361
            ad9361._set_ensm_mode(h, "alert")
            if getattr(self, "_was_tdd", False):
                ad9361._set_duplex_mode(h, fdd=False)     # boot default again
        except Exception as ex:                           # noqa: BLE001
            print(f"  ensm alert / duplex restore failed: {ex}")


def board_info(runner) -> dict:
    """ensm_mode, AD9361 and XADC temperature via sysfs (read only)."""
    if runner is None:
        return {"ensm": "fake", "adtemp": 41.0, "xadc": 41.0}
    info = {}
    try:
        for line in runner(["sh", "-c", _BOARD_INFO]).splitlines():
            k, _, v = line.partition(" ")
            info[k] = v.strip() if k == "ensm" else float(v) / (1000.0 if k == "adtemp" else 1.0)
    except Exception as ex:                               # noqa: BLE001
        info["error"] = str(ex)
    return info


def hottest(info: dict, radio_temp=None):
    v = [x for k, x in info.items() if k in ("adtemp", "xadc") and isinstance(x, float)]
    if radio_temp is not None:
        v.append(radio_temp)
    return max(v) if v else None


# ----------------------------------------------------------------- analysis
def align(c: np.ndarray, ref: np.ndarray):
    """Best placement of capture ``c`` in model stream ``ref`` (zero padded by
    len(c) both sides). Returns (offset, rho, gain, nrmse_db); offset = index in
    ``ref`` of c[0] (negative: c starts before the stream)."""
    n = len(c)
    rp = np.concatenate([np.zeros(n, np.complex64), ref, np.zeros(n, np.complex64)])
    size = 1 << int(math.ceil(math.log2(len(rp) + n)))
    r = np.fft.ifft(np.fft.fft(rp, size) * np.conj(np.fft.fft(c, size)))[:len(rp) - n + 1]
    cs = np.concatenate([[0.0], np.cumsum(np.abs(rp.astype(np.complex128)) ** 2)])
    wen = cs[n:n + len(r)] - cs[:len(r)]
    cn = float(np.linalg.norm(c))
    rho = np.where(wen > 1e-3 * wen.max(),
                   np.abs(r) / np.maximum(np.sqrt(np.maximum(wen, 0.0)) * cn, 1e-12), 0.0)
    l = int(np.argmax(rho))
    w = rp[l:l + n].astype(np.complex128)
    g = np.vdot(w, c) / max(np.vdot(w, w).real, 1e-12)
    err = float(np.linalg.norm(c - g * w))
    return l - n, float(rho[l]), g, 20 * math.log10(max(err / max(cn, 1e-12), 1e-12))


def channel_corr(band, fs, out_lsb, c, offset, nsv):
    """Per-channel |corr| of the capture with that PRN's model signal."""
    n = len(c)
    first = max(0, offset // B)
    last = min((offset + n - 1) // B, 10**9)
    chs = {}
    for idx, recs in generate_blocks(band, out_lsb=out_lsb, limit_fs=fs):
        if idx < first:
            continue
        if idx > last:
            break
        m = EngineModel(fs)
        for r in recs:
            _, ti, tq, _ = m.channel_terms(r)
            seg = np.zeros(n, np.complex128)
            lo = idx * B - offset
            a, b = max(lo, 0), min(lo + B, n)
            if b > a:
                seg[a:b] = (ti + 1j * tq)[a - lo:b - lo]
            chs.setdefault(r.prn, np.zeros(n, np.complex128))
            chs[r.prn] += seg
    cn = np.linalg.norm(c)
    return {p: float(abs(np.vdot(v, c)) / max(np.linalg.norm(v) * cn, 1e-12))
            for p, v in sorted(chs.items())}


def model_reference(band, fs, out_lsb, k):
    m = EngineModel(fs)
    out = [m.feed_block(i, ch) for i, ch in
           generate_blocks(band, n_blocks=k, out_lsb=out_lsb, limit_fs=fs)]
    y = np.concatenate(out).astype(np.float64)
    return (y[:, 0] + 1j * y[:, 1]).astype(np.complex64)


# ----------------------------------------------------------------- one case
def run_case(args, mode, fs, nsv, runner, make_device):
    name = f"{mode} {fs / 1e6:g}Msps nsv={nsv}"
    res = {"case": name, "mode": mode, "fs": fs, "nsv": nsv, "pass": False,
           "reasons": []}
    fake = args.fake
    tdd = mode == "tdd"
    if not tdd and fs > FDD_MAX_FS:
        res["reasons"].append("refused: FDD above 10 Msps")
        return res
    config.TX_TDD_MODE = bool(tdd)
    duration = args.duration if args.duration else (60.0 if tdd else 8.0)
    ref_blocks = args.ref_blocks
    total = 6 if fake else int(math.ceil(duration * fs / B)) + 1500   # outlasts the DMA lead and start-up
    band = make_band(fs, nsv, total, getattr(args, 'prns', None))
    cs = CommandStream(band, level_dbfs=args.level, group_blocks=args.group)
    dev = make_device()
    radio = FakeRadio(dev) if fake else RealRadio(args.uri, fs, not tdd, args.rx_lo_on)
    events, caps = [], []
    stop_rx = threading.Event()
    tx = None
    t_start = time.monotonic()
    temps = []
    info0 = board_info(runner)
    print(f"[{name}] start: ensm={info0.get('ensm')} ad9361={info0.get('adtemp')} C "
          f"xadc={info0.get('xadc')} C")
    res["start"] = info0
    try:
        radio.open()
        if not tdd:
            def rx_loop():
                while not stop_rx.is_set():
                    try:
                        a = radio.rx(1 << 17)
                    except Exception as ex:               # noqa: BLE001
                        res["reasons"].append(f"rx error: {ex}")
                        return
                    if len(caps) < 40 and np.abs(a).max(initial=0) > 0:
                        caps.append(a)
            threading.Thread(target=rx_loop, daemon=True).start()
            time.sleep(0.3)
        pre = Prefetch(iter(cs))
        tx = GnssTx(dev, radio.write_words, fs, progress_cb=events.append,
                    poll_s=1.0, prefill_blocks=args.prefill, mute=radio.close)
        tx.start(pre)
        t0 = time.monotonic()
        next_temp = t0
        hot = 0
        while time.monotonic() - t0 < duration:
            if fake and not tx.feeding:
                break
            if tx.error is not None:
                res["reasons"].append(f"feed error: {tx.error}")
                break
            if time.monotonic() >= next_temp:
                next_temp += 2.0
                tmax = hottest(board_info(runner), radio.temperature_c())
                if tmax is not None:
                    temps.append(tmax)
                    hot = hot + 1 if tmax > args.max_temp else 0
                    if hot >= 2:           # the AD9361 sensor glitches now and then
                        res["reasons"].append(f"ABORT: {tmax:.1f} C > {args.max_temp} C")
                        break
            time.sleep(0.2)
        final = tx.poll_once()
        st = tx.last_status
        res["gen_blocks_per_s"] = round(pre.count / max(time.monotonic() - t0, 1e-9), 1)
        res["required_blocks_per_s"] = round(fs / B, 1)
        if st is not None:
            res["counters"] = {k: getattr(st, k) for k in (
                "underruns", "dropped_late", "clipped", "chip_late", "bad_words",
                "sample_fifo_underflows", "cmd_fifo_overruns", "sample_fifo_level")}
            res["play_block"] = st.play_block
        if final:
            res["play_lag_s"] = final["gnss"]["play_lag_s"]
            res["lead_blocks_min"] = tx.min_lead
        res["timeline"] = [
            [round(e["elapsed_s"], 2), e["gnss"]["fed_blocks"], e["gnss"]["lead_blocks"],
             e["gnss"]["underruns"]] for e in events if "gnss" in e][::2]
        res["lead_low"] = tx.lead_low
        res["poll_errors"] = tx.poll_errors
    except Exception as ex:                                  # noqa: BLE001
        res["reasons"].append(f"{type(ex).__name__}: {ex}")
    finally:
        stop_rx.set()
        try:
            if tx is not None:
                tx.stop()
        except Exception as ex:                              # noqa: BLE001
            res["reasons"].append(f"stop: {ex}")
        radio.close()
        info1 = board_info(runner)
        res["end"] = info1
        print(f"[{name}] end:   ensm={info1.get('ensm')} ad9361={info1.get('adtemp')} C "
              f"xadc={info1.get('xadc')} C")
        if runner is not None and info1.get("ensm") not in ("alert",):
            res["reasons"].append(f"ensm_mode is {info1.get('ensm')!r}, not alert")
    res["temp_max_c"] = max(temps) if temps else None
    res["wall_s"] = round(time.monotonic() - t_start, 1)

    # ------------------------------------------------------- verdicts
    c = res.get("counters")
    if c is None:
        res["reasons"].append("no status read")
    else:
        bad = {k: v for k, v in c.items() if k != "sample_fifo_level" and v}
        if bad:
            res["reasons"].append(f"counters nonzero: {bad}")
    if res.get("lead_low") and not fake:
        res["reasons"].append(f"host lead below minimum {res['lead_low']}x")
    if not fake and res.get("gen_blocks_per_s", 1e9) < 0.9 * res.get("required_blocks_per_s", 0):
        res["reasons"].append("command generation slower than real time "
                              f"({res['gen_blocks_per_s']} < {res['required_blocks_per_s']} blk/s)")
    if not tdd:
        try:
            if not caps:
                res["reasons"].append("no non-zero RX capture (loopback/RX path?)")
            else:
                ref = model_reference(band, fs, cs.out_lsb, ref_blocks)
                best = None
                for a in caps[:6]:
                    off, rho, g, nr = align(a, ref)
                    if best is None or rho > best[1]:
                        best = (off, rho, g, nr, a)
                off, rho, g, nr, a = best
                res.update(nrmse_db=round(nr, 2), rx_offset_samples=off,
                           xcorr=round(rho, 4), gain=round(abs(g), 5),
                           gain_phase_deg=round(math.degrees(np.angle(g)), 2))
                if -len(a) // 2 < off < ref_blocks * B - len(a) // 2:
                    pc = channel_corr(band, fs, cs.out_lsb, a, off, nsv)
                    res["channel_corr"] = {str(p): round(v, 3) for p, v in pc.items()}
                    need = 0.5 / math.sqrt(nsv)
                    weak = [p for p, v in pc.items() if v < need]
                    if weak:
                        res["reasons"].append(f"weak channels {weak} (< {need:.2f})")
                if nr >= NRMSE_MAX_DB:
                    res["reasons"].append(f"NRMSE {nr:.1f} dB >= {NRMSE_MAX_DB} dB")
        except Exception as ex:                              # noqa: BLE001
            res["reasons"].append(f"analysis: {type(ex).__name__}: {ex}")
    res["pass"] = not res["reasons"]
    return res


# ----------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default=config.DEVICE_URI, help="libiio URI, ssh host derived from it")
    ap.add_argument("--base", type=lambda x: int(x, 0), default=config.GNSS_BASE_ADDR)
    ap.add_argument("--tdd-counters", action="store_true")
    ap.add_argument("--fs", type=float, nargs="+", help="sample rates, Hz")
    ap.add_argument("--nsv", type=int, nargs="+", help="GPS satellites per case")
    ap.add_argument("--prns", type=int, nargs="+", help="C/A PRNs to use (default 1..nsv); e.g. 120 126 131 for SBAS")
    ap.add_argument("--duration", type=float, help="seconds per case (8 loopback / 60 tdd)")
    ap.add_argument("--ref-blocks", type=int, default=12, help="model blocks used for alignment")
    ap.add_argument("--level", type=float, default=-15.0, help="level_dbfs of the engine output")
    ap.add_argument("--group", type=int, default=8, help="blocks per DMA buffer")
    ap.add_argument("--prefill", type=int, default=64)
    ap.add_argument("--max-temp", type=float, default=70.0)
    ap.add_argument("--rx-lo-on", action="store_true", help="power the RX LO in loopback")
    ap.add_argument("--json", default=str(pathlib.Path(__file__).with_name("fpga_bringup_result.json")))
    ap.add_argument("--fake", action="store_true", help="in-memory engine and radio, no board")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.ERROR if args.fake else logging.WARNING)
    mode = "tdd" if args.tdd_counters else "loopback"
    fss = args.fs or ([20e6, 30e6] if args.tdd_counters else [5e6, 10e6])
    nsvs = args.nsv or ([11] if args.tdd_counters else [1, 11])
    config.ALLOW_TX = True                      # explicit: this script is the TX test
    if args.fake:
        runner = None
        fss = [min(f, 10e6) if mode == "loopback" else f for f in fss]
        eng_kw = dict(keep_samples=True)
        make_device = lambda: D.FakeDevice(D.FakeEngine(**eng_kw))   # noqa: E731
    else:
        runner = D.SshRunner.from_uri(args.uri, user=config.GNSS_SSH_USER, timeout_s=20.0)
        make_device = lambda: D.RemoteDevmem(runner, args.base)       # noqa: E731
    results = []
    try:
        for fs in fss:
            for nsv in nsvs:
                results.append(run_case(args, mode, fs, nsv, runner, make_device))
                if any("ABORT" in r for r in results[-1]["reasons"]):
                    break
            else:
                continue
            break
    except KeyboardInterrupt:
        print("interrupted")
    print("\n%-34s %-5s %9s %8s %s" % ("case", "", "NRMSE dB", "temp C", "notes"))
    for r in results:
        print("%-34s %-5s %9s %8s %s" % (
            r["case"], "PASS" if r["pass"] else "FAIL", r.get("nrmse_db", "-"),
            r.get("temp_max_c") or "-", "; ".join(r["reasons"])))
    pathlib.Path(args.json).write_text(json.dumps(
        {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "args": vars(args),
         "results": results}, indent=1, default=str))
    print("json:", args.json)
    return 0 if results and all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
