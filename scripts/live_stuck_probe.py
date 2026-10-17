"""How often does the DAC run dry while the live pipeline streams?

When the board's DMA starves, the DAC keeps playing stale data; over the
digital loopback that shows up as a block whose samples barely move. Streams a
LiveSession through the real backend and takes many short loopback captures
(short, so the capture itself does not take the link from the TX stream).

    ALLOW_TX=1 TX_TDD_MODE=0 python scripts/live_stuck_probe.py --rate 30e6 --seconds 12
"""
import argparse, datetime as dt, threading, time
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=30e6)
    ap.add_argument("--kind", default="tezuka")
    ap.add_argument("--uri", default="ip:192.168.3.1")
    ap.add_argument("--gain", type=float, default=-50.0)
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--systems", default="G")
    ap.add_argument("--cap", type=int, default=16384)
    ap.add_argument("--complex-path", action="store_true", help="old path: scale/pack in the main process")
    ap.add_argument("--light", action="store_true", help="loop one pre-scaled segment: near-zero feeder CPU load")
    ap.add_argument("--rinex", default="D:/codes/gps-sim/data/rinex/BRDC_2026289.rnx")
    a = ap.parse_args()
    from backend import scenario
    from backend.rf import dual_tx, transmit
    from backend.rf.backends import ad9361
    from backend.session import live
    req = scenario.ScenarioRequest(rinex_path=a.rinex, lat=39.9179, lon=32.8627, alt=850.0,
                                   start=dt.datetime.utcnow(), duration_s=600,
                                   sample_rate=a.rate, sample_format="int16", systems=tuple(a.systems.upper().replace(",", "")))
    sess = live.LiveSession(req)
    params = transmit.TxParams(iq_path="", sample_rate=a.rate, sample_format="int16",
                               tx_gain_db=a.gain, uri=a.uri, kind=a.kind)
    result = {}
    from backend.rf import air
    fmt = air.air_format_for(a.kind, a.rate) if not a.complex_path else None
    src = (sess.air_segments(fmt, level_dbfs=params.level_dbfs, tx_scale=params.tx_scale)
           if fmt else sess.segments())
    if a.light:
        seg = next(src)
        sess.stop()
        rms = float(np.sqrt(np.mean(np.abs(seg[:1 << 20]) ** 2)))
        dac_fs = transmit._dac_full_scale(a.kind, a.rate)
        off = transmit._level_offset_db(a.kind, a.rate)
        seg = (seg * np.float32(dac_fs * 10 ** ((params.level_dbfs + off) / 20) / rms)).astype(np.complex64)
        params.level_dbfs = None
        def loop():
            while True:
                yield seg
        src = loop()
    th = threading.Thread(target=lambda: result.update(
        transmit.stream(params, chunk_source=src)), daemon=True)
    th.start()
    t0 = time.monotonic()
    while (dual_tx._card is None or not dual_tx._card.started) and time.monotonic() - t0 < 40:
        time.sleep(0.05)
    card = dual_tx._card
    h = card.handle
    time.sleep(1.0)
    h._ctrl.debug_attrs["loopback"].value = "1"
    ad9361._set_rx_lo_powerdown(h, False)
    h.rx_enabled_channels = [0]
    h.rx_buffer_size = a.cap
    h.rx()
    n = stuck = rx_fail = 0
    stds = []
    t_end = time.monotonic() + a.seconds
    while time.monotonic() < t_end:
        try:
            x = np.asarray(h.rx())
        except OSError as ex:
            print("rx failed:", ex)
            rx_fail += 1
            time.sleep(0.5)
            continue
        s = float(np.std(x.real)); stds.append(s)
        n += 1
        time.sleep(0.15)
    h._ctrl.debug_attrs["loopback"].value = "0"
    sess.stop(); th.join(timeout=30)
    ref = float(np.median(stds))
    stuck = sum(1 for s in stds if s < 0.2 * ref)
    print(f"{a.kind} {a.rate/1e6:.1f} Msps: {stuck}/{n} captures stuck (median std {ref:.0f}); "
          f"host underflow {result.get('underflow')} device_underrun {result.get('device_underrun')} "
          f"min_lead {result.get('min_lead_s')}")
    print("rx failures:", rx_fail)
    print("stds:", [round(s) for s in stds])


if __name__ == "__main__":
    main()
