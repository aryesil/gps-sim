"""End-to-end check of the pluto_cs8 backend with a real generated L1 file.

Streams the file through backend.rf.transmit (so the level scaling, int8
quantisation and dual_tx pump are the production path) at a capped TX gain,
with the AD9361 digital loopback on, captures one RX block from the same
stream and runs the project's own L1 acquisition on it. The satellites listed
in the file's meta.json must come back with a strong correlation peak.

    ALLOW_TX=1 python scripts/cs8_e2e.py out/<run>/gpssim.bin --kind pluto_cs8 --gain -50

TX gain is refused above --max-gain (default -50 dB).
"""
from __future__ import annotations

import argparse
import os
import json
import pathlib
import threading
import time

import numpy as np

# The loopback receives through the same chip; TDD's tx state switches RX off.
os.environ.setdefault("TX_TDD_MODE", "0")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("iq")
    ap.add_argument("--uri", default="ip:192.168.3.1")
    ap.add_argument("--kind", default="pluto_cs8", choices=("pluto_cs8", "pluto"))
    ap.add_argument("--gain", type=float, default=-50.0)
    ap.add_argument("--max-gain", type=float, default=-50.0)
    ap.add_argument("--loops", type=int, default=4)
    a = ap.parse_args()
    if a.gain > a.max_gain:
        raise SystemExit(f"refusing TX gain {a.gain} dB (> {a.max_gain} dB)")

    from backend.analysis.l1_acquire import acquire_l1
    from backend.rf import dual_tx, transmit

    path = pathlib.Path(a.iq)
    meta = json.loads((path.parent / "meta.json").read_text())
    rate = float(meta["sample_rate"])
    fmt = meta["sample_format"]
    import re
    prns = [int(x) for x in re.search(r"'prns': \[([^\]]*)\]", str(meta["provenance"])).group(1).split(",")]

    params = transmit.TxParams(iq_path=str(path), sample_rate=rate, sample_format=fmt,
                               tx_gain_db=a.gain, uri=a.uri, kind=a.kind,
                               chunk_samples=65536)

    def chunks():
        for _ in range(a.loops):
            yield from transmit._iter_chunks(str(path), fmt, params.chunk_samples)

    result = {}

    def run():
        result.update(transmit.stream(params, chunk_source=chunks()))

    th = threading.Thread(target=run, daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while dual_tx._card is None and time.monotonic() < deadline:
        time.sleep(0.05)
    card = dual_tx._card
    if card is None:
        th.join(1)
        print("stream failed to start:", result)
        return 2
    h = card.handle
    time.sleep(1.0)
    h._ctrl.debug_attrs["loopback"].value = "1"
    from backend.rf.backends import ad9361
    ad9361._set_rx_lo_powerdown(h, False)
    h.rx_enabled_channels = [0]
    h.rx_buffer_size = 262144
    h.rx()
    iq = np.asarray(h.rx())
    h._ctrl.debug_attrs["loopback"].value = "0"
    th.join(timeout=60)

    print(f"{a.kind}  gain {a.gain} dB  rate {rate/1e6:.2f} Msps  stream: {result}")
    ok = 0
    others = [p for p in range(1, 33) if p not in prns]
    for p in prns:
        r = acquire_l1(iq, rate, "G", p, centre_offset_hz=0.0)
        print(f"  PRN {p:2d}  metric {r['metric_db']:6.1f} dB  doppler {r['doppler_hz']:7.0f} Hz")
        ok += r["metric_db"] > 12
    absent = max(acquire_l1(iq, rate, "G", p, centre_offset_hz=0.0)["metric_db"] for p in others[:6])
    print(f"  absent PRNs max metric {absent:.1f} dB")
    print(f"acquired {ok}/{len(prns)}")
    return 0 if ok >= max(1, len(prns) - 2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
