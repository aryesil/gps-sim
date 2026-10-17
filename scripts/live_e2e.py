"""Digital-loopback check of the live pipeline at any sample rate.

Runs a LiveSession (the same generator the /api/live/start path uses) through
transmit.stream and the real backend for ``--seconds``, switches the AD9361
digital loopback on, captures one RX block from the stream and acquires every
GPS PRN in it. It proves the whole host + link + FPGA chain (including the
rate-dependent backend choice) delivers the generated signal, without needing a
receiver. TX gain stays at the caller's value; the RF output is not part of it.

    ALLOW_TX=1 TX_TDD_MODE=0 python scripts/live_e2e.py --rate 30e6 --kind tezuka
"""
from __future__ import annotations

import argparse
import datetime as dt
import threading
import time

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=30e6)
    ap.add_argument("--kind", default="tezuka")
    ap.add_argument("--uri", default="ip:192.168.3.1")
    ap.add_argument("--gain", type=float, default=-50.0)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--settle", type=float, default=1.0, help="s of stream before the loopback capture")
    ap.add_argument("--rinex", default="D:/codes/gps-sim/data/rinex/BRDC_2026289.rnx")
    a = ap.parse_args()
    if a.gain > -30.0:
        raise SystemExit("refusing TX gain above -30 dB")

    from backend import scenario
    from backend.analysis.l1_acquire import acquire_l1
    from backend.rf import dual_tx, transmit
    from backend.rf.backends import ad9361
    from backend.session import live

    req = scenario.ScenarioRequest(rinex_path=a.rinex, lat=39.9179, lon=32.8627, alt=850.0,
                                   start=dt.datetime.utcnow(), duration_s=300,
                                   sample_rate=a.rate, sample_format="int16", systems=("G",))
    sess = live.LiveSession(req)
    params = transmit.TxParams(iq_path="", sample_rate=a.rate, sample_format="int16",
                               tx_gain_db=a.gain, uri=a.uri, kind=a.kind)
    result: dict = {}
    events: list = []

    def run():
        result.update(transmit.stream(params, chunk_source=sess.segments(),
                                      progress_cb=events.append))

    th = threading.Thread(target=run, daemon=True)
    th.start()
    deadline = time.monotonic() + 30
    while dual_tx._card is None and time.monotonic() < deadline:
        time.sleep(0.05)
    card = dual_tx._card
    if card is None:
        print("stream failed to start", result)
        return 2
    h = card.handle
    t_end = time.monotonic() + a.seconds
    while not card.started and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(a.settle)
    h._ctrl.debug_attrs["loopback"].value = "1"
    ad9361._set_rx_lo_powerdown(h, False)
    h.rx_enabled_channels = [0]
    h.rx_buffer_size = 1 << 20
    h.rx()
    iq = np.asarray(h.rx())
    h._ctrl.debug_attrs["loopback"].value = "0"
    while time.monotonic() < t_end:
        time.sleep(0.1)
    import os
    if os.environ.get('E2E_SAVE'):
        np.save(os.environ['E2E_SAVE'], iq)
    sess.stop()
    th.join(timeout=30)
    print(f"rx rms I {np.std(iq.real):.1f} Q {np.std(iq.imag):.1f} mean {iq.mean():.1f} peak {np.abs(iq).max():.0f}")
    print(f"backend: {type(card.backend).__name__ if not hasattr(card.backend, '__name__') else card.backend.__name__}")
    loads = [e["feeder_load"] for e in events if "feeder_load" in e]
    qs = [e["queue_s"] for e in events if "queue_s" in e]
    print(f"underflow {result.get('underflow')} gapless {result.get('gapless')} "
          f"feeder_load max {max(loads) if loads else None} queue_s min {min(qs) if qs else None}")
    res = []
    for p in range(1, 33):
        r = acquire_l1(iq, a.rate, "G", p, centre_offset_hz=0.0)
        res.append((r["metric_db"], p, r["doppler_hz"]))
    res.sort(reverse=True)
    for m, p, d in res[:12]:
        print(f"  PRN {p:2d} metric {m:5.1f} dB doppler {d:7.0f} Hz")
    print("noise floor (median metric) %.1f dB" % np.median([r[0] for r in res]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
