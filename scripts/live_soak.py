"""Soak the live pipeline at a sample rate without a receiver and report the
real-time health numbers: host underflow, device-side underrun (dead
reckoning), lowest lead, queue fill and the feeder's CPU load.

    ALLOW_TX=1 python scripts/live_soak.py --rate 30e6 --seconds 40 --gain -80
"""
import argparse, datetime as dt, threading, time

ap = argparse.ArgumentParser()
ap.add_argument("--rate", type=float, default=30e6)
ap.add_argument("--kind", default="tezuka")
ap.add_argument("--uri", default="ip:192.168.3.1")
ap.add_argument("--gain", type=float, default=-80.0)
ap.add_argument("--seconds", type=float, default=30.0)
ap.add_argument("--systems", default="G")
ap.add_argument("--rinex", default="D:/codes/gps-sim/data/rinex/BRDC_2026289.rnx")


def main():
    a = ap.parse_args()

    from backend import scenario
    from backend.rf import transmit
    from backend.session import live

    req = scenario.ScenarioRequest(rinex_path=a.rinex, lat=39.9179, lon=32.8627, alt=850.0,
                                   start=dt.datetime.utcnow(), duration_s=600,
                                   sample_rate=a.rate, sample_format="int16", systems=tuple(a.systems.upper().replace(",", "")))
    sess = live.LiveSession(req)
    from backend.rf import air
    params = transmit.TxParams(iq_path="", sample_rate=a.rate, sample_format="int16",
                               tx_gain_db=a.gain, uri=a.uri, kind=a.kind)
    fmt = air.air_format_for(a.kind, a.rate)
    src = (sess.air_segments(fmt, level_dbfs=params.level_dbfs, tx_scale=params.tx_scale)
           if fmt else sess.segments())
    events, result = [], {}
    th = threading.Thread(target=lambda: result.update(
        transmit.stream(params, chunk_source=src, progress_cb=events.append)), daemon=True)
    th.start()
    time.sleep(a.seconds)
    sess.stop()
    th.join(timeout=30)
    ev = [e for e in events if "feeder_load" in e]
    print(f"{a.kind} {a.rate/1e6:.1f} Msps {a.seconds:.0f} s: {result}")
    print("feeder_load max %.2f mean %.2f | queue_s min %s | lead_s min %s" % (
        max(e["feeder_load"] for e in ev), sum(e["feeder_load"] for e in ev) / len(ev),
        min((e["queue_s"] for e in ev if "queue_s" in e), default=None),
        min((e["lead_s"] for e in ev if "lead_s" in e), default=None)))
    print("lead_s series:", [e.get("lead_s") for e in ev][:30])


if __name__ == "__main__":
    main()
