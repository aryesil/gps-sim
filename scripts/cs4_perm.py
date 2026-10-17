import os, sys, time
import numpy as np
os.environ.setdefault("TX_TDD_MODE", "0"); os.environ.setdefault("ALLOW_TX", "1")
import iio
from backend.rf.backends import ad9361, ad9361_cs4, ad9361_cs8
mode = sys.argv[1]; L = int(sys.argv[2]) if len(sys.argv) > 2 else 64
mod = ad9361_cs4 if mode == "cs4" else ad9361_cs8
code = 3 if mode == "cs4" else 20
h = mod.open_tx("ip:192.168.3.1", 1575.42e6, 4e6)
try:
    mod.set_gain(h, 0, -50.0)
    ad9361._set_rx_lo_powerdown(h, False)
    h._ctrl.debug_attrs["loopback"].value = "1"
    h.rx_enabled_channels = [0]; h.rx_buffer_size = L * 16
    out = []
    for p in range(L):
        blk = np.zeros(L, np.complex64); blk[p] = code
        raw = mod.pack_cs4(blk) if mode == "cs4" else mod.pack_cs8(blk)
        h._gs_cs4_buf = None
        buf = iio.Buffer(h._txdac, len(raw) // 2, True); buf.write(bytearray(raw)); buf.push()
        time.sleep(0.25); h.rx(); x = np.asarray(h.rx())
        y = x.real[:L * 8].reshape(8, L).mean(0)
        base = np.median(y)
        dev = y - base
        idx = np.nonzero(np.abs(dev) > 1)[0]
        out.append((p, [(int(i), int(dev[i])) for i in idx[:6]], int(base)))
        del buf
    for o in out: print(o)
finally:
    try: h._ctrl.debug_attrs["loopback"].value = "0"
    except Exception: pass
    mod.close(h)
