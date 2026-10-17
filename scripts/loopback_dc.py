"""Digital-loopback DC / balance check for a backend at a given rate.

Pushes zero blocks (and, with --noise, random noise at a fixed level) through the
backend, turns the AD9361 digital loopback on and reports the mean and rms of
the received I/Q. A non-zero mean with zero input is an artifact of the
receive path or of the TX data path itself, independent of the host pipeline.

    ALLOW_TX=1 TX_TDD_MODE=0 python scripts/loopback_dc.py cs4 30 --seconds 2
"""
import argparse, threading, time
import numpy as np
from backend.rf.backends import ad9361, ad9361_cs4, ad9361_cs8

ap = argparse.ArgumentParser()
ap.add_argument("mode", choices=("cs16", "cs8", "cs4"))
ap.add_argument("rate_msps", type=float)
ap.add_argument("--noise", action="store_true")
ap.add_argument("--periodic", type=int, default=0, help="repeat a random pattern of this many samples; report the fraction of samples that differ from the per-position median")
ap.add_argument("--uri", default="ip:192.168.3.1")
a = ap.parse_args()
mod = {"cs16": ad9361, "cs8": ad9361_cs8, "cs4": ad9361_cs4}[a.mode]
fs = a.rate_msps * 1e6
full = float(mod.DAC_FULL_SCALE)
h = mod.open_tx(a.uri, 1575.42e6, fs)
stop = threading.Event()
rng = np.random.default_rng(1)
per = a.periodic or 1
pat = np.tile(((rng.standard_normal(per) + 1j * rng.standard_normal(per)) * (0.25 * full)).astype(np.complex64), 65536 // per) if a.periodic else None
def feeder():
    n = 65536
    while not stop.is_set():
        if a.periodic:
            blk = pat
        elif a.noise:
            blk = ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * (0.25 * full)).astype(np.complex64)
        else:
            blk = np.zeros(n, np.complex64)
        mod.write(h, [blk])
try:
    mod.set_gain(h, 0, -50.0)
    th = threading.Thread(target=feeder, daemon=True); th.start()
    time.sleep(0.5)
    h._ctrl.debug_attrs["loopback"].value = "1"
    ad9361._set_rx_lo_powerdown(h, False)
    h.rx_enabled_channels = [0]; h.rx_buffer_size = 1 << 19
    h.rx(); x = np.asarray(h.rx())
    h._ctrl.debug_attrs["loopback"].value = "0"
    if a.periodic:
        m = (len(x) // per) * per
        r = x[:m].reshape(-1, per)
        med_r = np.median(r.real, axis=0); med_i = np.median(r.imag, axis=0)
        bad = (r.real != med_r) | (r.imag != med_i)
        print(f"{a.mode} {a.rate_msps} Msps periodic {per}: {bad.mean()*100:.3f} % samples differ from per-position median ({bad.sum()} of {bad.size})")
    print(f"{a.mode} {a.rate_msps} Msps {'noise' if a.noise else 'zeros'}: mean {x.mean():.1f} rmsI {x.real.std():.1f} rmsQ {x.imag.std():.1f}")
finally:
    stop.set(); time.sleep(0.3)
    try: h._ctrl.debug_attrs["loopback"].value = "0"
    except Exception: pass
    mod.close(h)
