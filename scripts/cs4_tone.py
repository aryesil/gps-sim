"""cs4 / cs8 / cs16 digital-loopback tone check (no GNSS): complex tone at +f0 through the
production stream path, RX spectrum peak and image rejection from the loopback capture.
    ALLOW_TX=1 python scripts/cs4_tone.py --kind pluto_cs4 --gain -50 --rate 20e6 --f0 1e6
"""
import argparse, os, threading, time
import numpy as np
os.environ.setdefault("TX_TDD_MODE", "0")

ap = argparse.ArgumentParser()
ap.add_argument("--kind", default="pluto_cs4")
ap.add_argument("--uri", default="ip:192.168.3.1")
ap.add_argument("--gain", type=float, default=-50.0)
ap.add_argument("--rate", type=float, default=20e6)
ap.add_argument("--f0", type=float, default=1e6)
ap.add_argument("--dc", default="")
ap.add_argument("--secs", type=float, default=2.0)
a = ap.parse_args()
if a.gain > -50.0:
    raise SystemExit("refusing TX gain above -50 dB")

from backend.rf import dual_tx, transmit
N = 65536
k = np.arange(N)
# integer number of cycles per chunk so chunks join seamlessly
cyc = max(1, round(a.f0 * N / a.rate)); f0 = cyc * a.rate / N
tone = (10000 * np.exp(2j * np.pi * cyc * k / N)).astype(np.complex64)
if a.dc:
    re_, im_ = (float(v) for v in a.dc.split(","))
    tone = np.full(N, re_ + 1j * im_, np.complex64)
nchunks = int(a.secs * a.rate / N)
params = transmit.TxParams(iq_path="", sample_rate=a.rate, sample_format="int16",
                           tx_gain_db=a.gain, uri=a.uri, kind=a.kind, chunk_samples=N, level_dbfs=-15.0)
def chunks():
    for _ in range(nchunks):
        yield tone
result = {}
th = threading.Thread(target=lambda: result.update(transmit.stream(params, chunk_source=chunks())), daemon=True)
th.start()
t0 = time.monotonic()
while dual_tx._card is None and time.monotonic() - t0 < 10:
    time.sleep(0.05)
card = dual_tx._card
if card is None:
    th.join(1); print("stream failed to start:", result); raise SystemExit(2)
h = card.handle
time.sleep(0.8)
h._ctrl.debug_attrs["loopback"].value = "1"
from backend.rf.backends import ad9361
ad9361._set_rx_lo_powerdown(h, False)
h.rx_enabled_channels = [0]
h.rx_buffer_size = 1 << 17
h.rx()
iq = np.asarray(h.rx())
h._ctrl.debug_attrs["loopback"].value = "0"
th.join(timeout=60)
print(a.kind, "stream:", {x: result.get(x) for x in ("underflow", "clipped", "samples")})
if os.environ.get("E2E_SAVE"): np.save(os.environ["E2E_SAVE"], iq)
if a.dc:
    print(f"DC rx I {iq.real.mean():.1f}+-{iq.real.std():.1f}  Q {iq.imag.mean():.1f}+-{iq.imag.std():.1f}")
    raise SystemExit(0)
x = iq - iq.mean()
w = np.hanning(len(x)); S = np.abs(np.fft.fftshift(np.fft.fft(x * w))) ** 2
fr = np.fft.fftshift(np.fft.fftfreq(len(x), 1 / a.rate))
i0 = np.argmax(S); p0 = S[i0]
print(f"rx rms {np.std(x):.1f}  peak at {fr[i0]/1e6:+.4f} MHz (tx {f0/1e6:+.4f})")
def band(f): m = np.abs(fr - f) < 20e3; return S[m].max() if m.any() else 0
print(f"image at -f0: {10*np.log10(band(-f0)/p0+1e-30):6.1f} dBc; noise floor median {10*np.log10(np.median(S)/p0):6.1f} dBc")
top = np.argsort(S)[-6:][::-1]
print("top bins:", [(round(fr[i]/1e6, 3), round(10*np.log10(S[i]/p0), 1)) for i in top])
