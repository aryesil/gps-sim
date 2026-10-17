"""cs4 system identification: random 4-bit I codes (Q=0), cyclic; RX loopback; circular
cross-correlation shows the effective response in code units (lag structure, gain, offset)."""
import os, sys, time
import numpy as np
os.environ.setdefault("TX_TDD_MODE", "0"); os.environ.setdefault("ALLOW_TX", "1")
import iio
from backend.rf.backends import ad9361, ad9361_cs4
rate = float(sys.argv[1]) * 1e6 if len(sys.argv) > 1 else 4e6
comp = sys.argv[2] if len(sys.argv) > 2 else "i"
P = 1024
rng = np.random.default_rng(3)
a = rng.integers(-7, 8, P).astype(np.float32)
h = ad9361_cs4.open_tx("ip:192.168.3.1", 1575.42e6, rate)
try:
    ad9361_cs4.set_gain(h, 0, -50.0)
    ad9361._set_rx_lo_powerdown(h, False)
    h._ctrl.debug_attrs["loopback"].value = "1"
    h.rx_enabled_channels = [0]; h.rx_buffer_size = P * 16
    blk = (a + 0j) if comp == "i" else (1j * a)
    raw = ad9361_cs4.pack_cs4(blk.astype(np.complex64))
    h._gs_cs4_buf = None
    buf = iio.Buffer(h._txdac, len(raw) // 2, True); buf.write(bytearray(raw)); buf.push()
    time.sleep(0.5); h.rx(); x = np.asarray(h.rx())
    y = (x.real if comp == "i" else x.imag)[:P * 8].astype(np.float64)
    other = (x.imag if comp == "i" else x.real)
    Y = y.reshape(8, P).mean(0)          # fold: periodic part (if RX is aligned to P)
    print("fold consistency (std across periods / std total):", float(np.std(y.reshape(8, P) - Y) / np.std(y)))
    A = a - a.mean(); Yc = Y - Y.mean()
    c = np.fft.ifft(np.fft.fft(Yc) * np.conj(np.fft.fft(A))).real / (A @ A)
    lag = np.argsort(-np.abs(c))[:8]
    print("DC of rx:", Y.mean(), " other channel rms:", float(np.std(other)))
    print("top lags (lag, gain per code):", [(int(l), round(float(c[l]), 1)) for l in lag])
    res = Yc - np.real(np.fft.ifft(np.fft.fft(A) * np.fft.fft(c)))
    print("residual rms / signal rms:", float(np.std(res) / np.std(Yc)))
    del buf
finally:
    try: h._ctrl.debug_attrs["loopback"].value = "0"
    except Exception: pass
    ad9361_cs4.close(h)
