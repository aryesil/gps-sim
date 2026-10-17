"""cs4 code map via digital loopback: constant (I,Q) 4-bit codes -> RX mean (TX gain capped at -50 dB)."""
import os, sys, time
import numpy as np
os.environ.setdefault("TX_TDD_MODE", "0")
os.environ.setdefault("ALLOW_TX", "1")
import iio
from backend.rf.backends import ad9361, ad9361_cs4

pairs = [tuple(int(v) for v in a.split(",")) for a in sys.argv[1:]] or \
    [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (7, 0), (-1, 0), (-4, 0), (-7, 0), (0, 1), (0, 4), (1, 1), (4, 4), (-4, -4), (7, 7)]
h = ad9361_cs4.open_tx("ip:192.168.3.1", 1575.42e6, 20e6)
try:
    ad9361_cs4.set_gain(h, 0, -50.0)
    ad9361._set_rx_lo_powerdown(h, False)
    h._ctrl.debug_attrs["loopback"].value = "1"
    h.rx_enabled_channels = [0]
    h.rx_buffer_size = 1 << 15
    for i, q in pairs:
        raw = ad9361_cs4.pack_cs4(np.full(4096, i + 1j * q, np.complex64))
        h._gs_cs4_buf = None
        buf = iio.Buffer(h._txdac, len(raw) // 2, True)
        buf.write(bytearray(raw)); buf.push()
        time.sleep(0.4)
        h.rx(); x = np.asarray(h.rx())
        print(f"code I={i:3d} Q={q:3d} (byte {(q & 15) << 4 | (i & 15):02x}) -> rx I {x.real.mean():8.1f}  Q {x.imag.mean():8.1f}  sd {x.std():.1f}", flush=True)
        del buf
finally:
    try: h._ctrl.debug_attrs["loopback"].value = "0"
    except Exception: pass
    ad9361_cs4.close(h)
