import os, sys, time
import numpy as np
os.environ.setdefault("TX_TDD_MODE", "0"); os.environ.setdefault("ALLOW_TX", "1")
import iio
from backend.rf.backends import ad9361, ad9361_cs4, ad9361_cs8
mode = sys.argv[1]; pos = int(sys.argv[2]) if len(sys.argv) > 2 else 10; comp = sys.argv[3] if len(sys.argv) > 3 else "i"
mod = ad9361_cs4 if mode == "cs4" else ad9361_cs8
full = 7 if mode == "cs4" else 100
h = mod.open_tx("ip:192.168.3.1", 1575.42e6, 4e6)
try:
    mod.set_gain(h, 0, -50.0)
    ad9361._set_rx_lo_powerdown(h, False)
    h._ctrl.debug_attrs["loopback"].value = "1"
    h.rx_enabled_channels = [0]; h.rx_buffer_size = 4096
    for code in range(-7, 8):
        blk = np.zeros(64, np.complex64); blk[10] = code
        raw = mod.pack_cs4(blk)
        h._gs_cs4_buf = None
        buf = iio.Buffer(h._txdac, len(raw) // 2, True); buf.write(bytearray(raw)); buf.push()
        time.sleep(0.3); h.rx(); x = np.asarray(h.rx())[:640]
        y = x.real[:512].reshape(8, 64).mean(0) if False else x.real[:512]
        base = np.median(y); dev = y - base; i = np.argmax(np.abs(dev))
        print(f"code {code:3d} (0x{code & 15:x}) -> dev {dev[i]:7.0f}  base {base:.0f}  nz={int((np.abs(dev)>1).sum())}  (/code {dev[i]/code if code else 0:.1f})", flush=True)
        del buf
finally:
    try: h._ctrl.debug_attrs["loopback"].value = "0"
    except Exception: pass
    mod.close(h)
