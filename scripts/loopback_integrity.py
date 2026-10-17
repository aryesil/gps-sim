"""Stream integrity over the AD9361 digital loopback at a given rate.

Pushes a long deterministic noise sequence (no repeats within the capture)
through a backend with the real pump/queue (dual_tx), captures a block from the
loopback and compares it with the sequence window by window: a gap, a dropped
or repeated sample, or a bit error shows up as a window whose correlation with
the expected samples collapses. The mean (DC) of the received block is reported
as well.

    ALLOW_TX=1 TX_TDD_MODE=0 python scripts/loopback_integrity.py cs4 30
"""
import argparse, time
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("kind", choices=("pluto", "pluto_cs8", "pluto_cs4"))
ap.add_argument("rate_msps", type=float)
ap.add_argument("--gain", type=float, default=-50.0)
ap.add_argument("--uri", default="ip:192.168.3.1")
ap.add_argument("--capture", type=int, default=1 << 19)
ap.add_argument("--window", type=int, default=4096)
a = ap.parse_args()

from backend.rf import dual_tx
from backend.rf.backends import BACKENDS, ad9361

fs = a.rate_msps * 1e6
backend = BACKENDS[a.kind]
full = float(backend.DAC_FULL_SCALE)
N = 1 << 23                                   # 8.4 M samples of sequence, looped
rng = np.random.default_rng(7)
seq = ((rng.standard_normal(N) + 1j * rng.standard_normal(N)) * (0.3 * full)).astype(np.complex64)

sink = dual_tx.acquire("TX1", a.uri, 1575.42e6, fs, a.gain, a.kind)
import threading
stop = threading.Event()
def feed():
    pos = 0
    step = 1 << 19
    while not stop.is_set():
        sink.push(seq[pos:pos + step])
        pos = (pos + step) % N
th = threading.Thread(target=feed, daemon=True); th.start()
card = dual_tx._card
try:
    while not card.started:
        time.sleep(0.05)
    time.sleep(0.5)
    h = card.handle
    h._ctrl.debug_attrs["loopback"].value = "1"
    ad9361._set_rx_lo_powerdown(h, False)
    h.rx_enabled_channels = [0]; h.rx_buffer_size = a.capture
    h.rx(); x = np.asarray(h.rx())
    h._ctrl.debug_attrs["loopback"].value = "0"
finally:
    stop.set(); time.sleep(0.2)
    uf = sink.underflow
    try: sink.close()
    except Exception: pass

# align: correlate the first window against the sequence
w = a.window
ref = x.real[:w].astype(np.float64); ref -= ref.mean()
sr = seq.real.astype(np.float64)
F = np.fft.rfft(np.concatenate([sr, sr[:w]]))
G = np.fft.rfft(ref, len(F) * 2 - 2)
cc = np.fft.irfft(F * np.conj(G), len(F) * 2 - 2)[:N]
off = int(np.argmax(cc))
exp = np.concatenate([seq, seq])[off:off + len(x)]
bad = 0; nwin = len(x) // w
firstbad = None
for k in range(nwin):
    s = slice(k * w, (k + 1) * w)
    c = np.corrcoef(x.real[s], exp.real[s])[0, 1]
    if c < 0.95:
        bad += 1
        if firstbad is None: firstbad = (k, round(float(c), 3))
print(f"{a.kind} {a.rate_msps} Msps: windows bad {bad}/{nwin} (first {firstbad}); mean {x.mean():.1f} rmsI {x.real.std():.1f} rmsQ {x.imag.std():.1f}; pump underflow {uf}")
