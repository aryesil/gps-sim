# SPDX-License-Identifier: MIT
"""Measure the board's own oscillator error from real GNSS signals on its RX port.

    python scripts/board_rx_clock.py --lat 39.9179 --lon 32.8627 --alt 850 --seconds 30

Needs a GNSS antenna (active: bias-T) on RX A and the PC clock within about a second of UTC.
Sets the AD9361 to L1 / 4 Msps / manual gain / ENSM rx, streams IQ over libiio (`iio_readdev`),
runs the gnss_rx receiver and prints the fractional oscillator error. The ENSM goes back to
alert at the end. The result is relative to the ``xo_correction`` currently set in the driver;
the total error is reported as the value to give ``osc_error_ppm`` of a live start.
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
import threading
import warnings

import numpy as np

from backend.rf.doppler import ecef, make_predictor
from gnss_rx.measure import measure_oscillator

L1 = 1575.42e6


def _ssh(host: str, cmd: str) -> str:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"root@{host}", cmd],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ssh failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _attr(host: str, args: str) -> str:
    return _ssh(host, f"iio_attr {args} | tail -1")


class BoardRx:
    def __init__(self, host: str, fs: float, gain_db: float, lo_hz: float = L1, fdd: bool = False):
        self.host, self.fs, self.gain, self.lo, self.fdd = host, fs, gain_db, lo_hz, fdd
        self.proc = None
        self.t_first = None
        self.save = None

    def setup(self):
        h = self.host
        self.xo_hz = int(_attr(h, "-d ad9361-phy xo_correction"))
        self.agc = _attr(h, "-c ad9361-phy -i voltage0 gain_control_mode")
        if not self.fdd:
            _attr(h, "-d ad9361-phy ensm_mode alert")
        _attr(h, f"-c ad9361-phy -i voltage0 sampling_frequency {int(self.fs)}")
        # a transmit session powers the RX LO down; it must be switched on again before tuning
        _ssh(h, "iio_attr -c ad9361-phy altvoltage0 powerdown 0 >/dev/null; "
                f"iio_attr -c ad9361-phy altvoltage0 frequency {int(self.lo)} >/dev/null")
        _ssh(h, "iio_attr -c ad9361-phy -i voltage0 rf_port_select A_BALANCED >/dev/null 2>&1; "
                "iio_attr -c ad9361-phy -i voltage0 gain_control_mode manual >/dev/null; "
                f"iio_attr -c ad9361-phy -i voltage0 hardwaregain {self.gain} >/dev/null")
        if not self.fdd:
            _attr(h, "-d ad9361-phy ensm_mode rx")

    def stop(self):
        if self.proc is not None:
            self.proc.kill()
            self.proc = None
        try:
            if not self.fdd:
                _attr(self.host, "-d ad9361-phy ensm_mode alert")
            if getattr(self, "agc", None):
                _attr(self.host, f"-c ad9361-phy -i voltage0 gain_control_mode {self.agc}")
        except Exception as ex:
            print(f"ensm alert restore failed: {ex}", file=sys.stderr)

    def start_capture(self, seconds: float):
        n = int(self.fs * seconds) + int(self.fs * 0.5)
        self.proc = subprocess.Popen(
            ["iio_readdev", "-u", f"ip:{self.host}", "-b", "262144", "-s", str(n),
             "cf-ad9361-lpc", "voltage0", "voltage1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # Drain the pipe in a thread: if the processing stalls the RX DMA overruns and the data
        # freezes, so the reader must never wait for the consumer.
        self._chunks, self._have, self._eof = [], 0, False
        self._cv = threading.Condition()

        def pump():
            while True:
                b = self.proc.stdout.read(1 << 20)
                with self._cv:
                    if not b:
                        self._eof = True
                    else:
                        self._chunks.append(b)
                        self._have += len(b)
                    self._cv.notify_all()
                if not b:
                    return
        threading.Thread(target=pump, daemon=True).start()

    def read(self, n: int) -> np.ndarray:
        need = 4 * n
        with self._cv:
            while self._have < need and not self._eof:
                self._cv.wait(1.0)
            data = b"".join(self._chunks)
            buf, rest = data[:need], data[need:]
            self._chunks = [rest] if rest else []
            self._have = len(rest)
        if self.t_first is None:
            self.t_first = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        if self.save is not None:
            self.save.write(buf)
        if len(buf) < need:
            raise EOFError("capture ended early")
        v = np.frombuffer(buf, np.int16).astype(np.float32)
        if v.size and np.all(v[0::2] == v[0]) and np.all(v[1::2] == v[1]):
            raise RuntimeError("RX data is frozen at a constant value (RX path off / ENSM state?)")
        return v[0::2] + 1j * v[1::2]


def measure(host: str, lat: float, lon: float, alt: float, seconds: float = 30.0, fs: float = 4e6,
            gain: float = 60.0, fdd: bool = False, weak: bool = True, save: str | None = None,
            prns=range(1, 33), progress=None):
    """Capture ``seconds`` of RX IQ and return ``(Measurement | None, xo_hz)``; ``xo_hz`` is the
    driver's ``xo_correction`` during the capture."""
    rx = BoardRx(host, fs, gain, fdd=fdd)
    try:
        rx.setup()
        # Everything slow (imports, ephemeris, predictor) happens before the capture starts.
        from backend.ephem.ephemeris import get_ephemeris
        t_ref = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            eph = get_ephemeris(t_ref.date(), download=False)
        base = make_predictor(t_ref, ecef(lat, lon, alt), eph)
        base(next(iter(eph)), 0.0)                              # warm the caches

        def predict(prn, t):
            return base(prn, t + (rx.t_first - t_ref).total_seconds())
        rx.start_capture(seconds)
        if save:
            rx.save = open(save, "wb")
        m = measure_oscillator(rx.read, fs, predict, [p for p in prns if p in eph], seconds, weak=weak,
                               progress=progress)
    finally:
        rx.stop()
        if rx.save is not None:
            rx.save.close()
            open(save + ".utc", "w").write(rx.t_first.isoformat())
    return m, rx.xo_hz


def total_error_ppm(y: float, xo_hz: int) -> float:
    """Oscillator error to give ``osc_error_ppm``: ``y`` is relative to the reference the driver
    assumed (``xo_correction``)."""
    xo_ppm = (xo_hz / 40e6 - 1.0) * 1e6
    return ((1 + xo_ppm * 1e-6) * (1 + y) - 1) * 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.3.1")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--alt", type=float, default=0.0)
    ap.add_argument("--fs", type=float, default=4e6)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--gain", type=float, default=60.0)
    ap.add_argument("--fdd", action="store_true", help="chip is in FDD with TX running: leave the ENSM alone")
    ap.add_argument("--save", help="also write the raw int16 IQ capture and its UTC start to this file")
    ap.add_argument("--fast", action="store_true", help="short acquisition (strong signals only)")
    ap.add_argument("--prns", type=int, nargs="*", default=list(range(1, 33)))
    a = ap.parse_args()
    m, xo_hz = measure(a.host, a.lat, a.lon, a.alt, a.seconds, a.fs, a.gain, a.fdd, not a.fast, a.save, a.prns)
    if m is None:
        print("no satellites acquired / no clock fix (antenna, bias-T, gain?)")
        sys.exit(2)
    print(f"xo_correction {xo_hz} Hz, {a.fs / 1e6:g} Msps, gain {a.gain} dB")
    print(f"acquired {m.acquired}")
    for t, f in m.windows:
        print(f"  t={t:5.1f}s y={f.y * 1e9:+10.3f} ppb sigma={f.sigma * 1e9:6.3f} nsv={len(f.used)} rej={f.rejected}")
    print(f"y = {m.y * 1e6:+.6f} ppm  (sem {m.sigma * 1e9:.3f} ppb, spread {m.spread * 1e9:.3f} ppb)")
    print(f"osc_error_ppm for a live start: {total_error_ppm(m.y, xo_hz):+.6f}")


if __name__ == "__main__":
    main()
