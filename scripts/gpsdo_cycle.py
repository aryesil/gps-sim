# SPDX-License-Identifier: MIT
"""One GPSDO cycle: measure the oscillator from real GNSS, then transmit with it corrected.

    python scripts/gpsdo_cycle.py --lat 39.9179 --lon 32.8627 --alt 850 --duration 120
    python scripts/gpsdo_cycle.py ... --osc-error -3.5223      # skip the measurement
    python scripts/gpsdo_cycle.py ... --no-rx                   # no RX: start error predicted from the last run
    python scripts/gpsdo_cycle.py ... --demo                    # synthetic IQ, no board RX, no TX

1. The board receives L1 (ENSM rx) and gnss_rx estimates the oscillator error ``y``.
2. The measured error is the transmission start error; the thermal drift ramp while transmitting
   comes from the holdover model (``backend.rf.holdover``) as a function of that error.
3. A live start is posted to the running backend with ``osc_error_ppm`` and the ramp.

The live state (satellites, y history, holdover prediction) is published to the MQTT broker on the
board for the Dashboard GPSDO page (``backend.rf.gpsdo_status``); ``--no-mqtt`` turns it off.
``--demo`` replaces the board RX by synthetic IQ and marks the status as demo.

Needs the backend running (``uvicorn backend.app:app --port 8001`` with ALLOW_TX=1), a powered
GNSS antenna on RX for step 1, and the PC clock close to UTC.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])
from backend.rf.gpsdo_status import StatusPublisher           # noqa: E402
from backend.rf.holdover import DEFAULT_PATH, Holdover                     # noqa: E402
from scripts.board_rx_clock import measure, total_error_ppm  # noqa: E402


def build_body(args, osc_ppm: float, rate: float, accel: float) -> dict:
    return {"confirm_isolated": True, "kind": "tezuka_gnss", "systems": ["G"], "sample_rate": int(args.fs),
            "sample_format": "int16", "lat": args.lat, "lon": args.lon, "alt": args.alt,
            "start_utc": "now", "tx_gain_db": args.tx_gain, "osc_error_ppm": round(osc_ppm, 6),
            "osc_drift_ppb_per_s": round(rate, 6), "osc_drift_accel_ppb_per_s2": round(accel, 9),
            "max_duration_s": args.duration}


L1_HZ_PER_PPB = 1575.42e6 * 1e-9          # carrier shift at L1 per ppb of oscillator error


def decision(ho, e0_ppm: float, rate: float, accel: float, duration: float, lat: float) -> dict:
    """Everything the holdover model decides from the measured start error, with the reasons.
    ``_log`` carries the human readable lines for the Dashboard log."""
    import numpy as np
    from backend.rf.xo import split_osc_error
    xo_ppm, residual = split_osc_error(e0_ppm)
    xo_hz = int(round(40e6 * (1.0 + xo_ppm * 1e-6)))
    lo_shift_hz = -residual * 1575.42e6
    d = (ho.asymptote_ppm - e0_ppm) * 1e3
    t = np.arange(0.0, duration + 1.0, 1.0)
    quad = rate * t + 0.5 * accel * t * t
    err = np.abs(quad - ho.error_ppb(e0_ppm, t))
    bad = np.nonzero(err > 0.5)[0]
    valid_s = float(t[bad[0]]) if len(bad) else float(duration)
    dec = {"e0_ppm": round(e0_ppm, 6), "asymptote_ppm": ho.asymptote_ppm, "tau_s": ho.tau_heat_s,
           "to_travel_ppb": round(d, 2), "rate_ppb_s": round(rate, 5), "accel_ppb_s2": round(accel, 9),
           "order": "quadratic fit over the run", "valid_s": valid_s, "max_err_ppb": round(float(err.max()), 3),
           "xo_hz": xo_hz, "residual_ppb": round(residual * 1e9, 3), "lo_shift_hz": round(lo_shift_hz, 2),
           "end_ppb": round(float(ho.error_ppb(e0_ppm, [duration])[0]), 2)}
    heat = "cool board: large drift expected" if d < -25 else (
        "board near its settled state: small drift expected" if abs(d) < 10 else "warm board: moderate drift expected")
    log = [("model", f"{heat}; error will move {d:+.1f} ppb towards {ho.asymptote_ppm:+.4f} ppm (tau {ho.tau_heat_s:.0f} s)"),
           ("model", f"ramp commanded: {rate:+.4f} ppb/s, curvature {accel * 1e5:+.2f}e-5 ppb/s2 (quadratic fitted over the planned run)"),
           ("apply", f"driver xo_correction {xo_hz} Hz, leftover {residual * 1e9:+.2f} ppb moves the TX LO by {lo_shift_hz:+.1f} Hz")]
    if valid_s < duration:
        log.append(("warn", f"quadratic ramp is within 0.5 ppb for {valid_s:.0f} s only; the {duration:.0f} s run is at most "
                            f"{float(err.max()):.2f} ppb off the exponential"))
    dec["_log"] = log
    return dec


def demo_measure(fs, seconds, y_true, progress):
    """Measurement on synthetic IQ (4 satellites): same code path, no hardware."""
    from gnss_rx.measure import measure_oscillator
    from gnss_rx.synth_iq import SvSpec, SynthIQ
    svs = [SvSpec(3, 1800.0, 45.0, doppler_rate_hz_s=-0.3, code_phase_chips=11.0),
           SvSpec(8, -3200.0, 44.0, doppler_rate_hz_s=0.5, code_phase_chips=300.0),
           SvSpec(14, 2500.0, 43.0, doppler_rate_hz_s=-0.4, code_phase_chips=620.0),
           SvSpec(22, -700.0, 44.0, doppler_rate_hz_s=0.2, code_phase_chips=900.0)]
    src = SynthIQ(fs, svs, clock_y=y_true)

    def predict(prn, tm):
        s = next(v for v in svs if v.prn == prn)
        return s.doppler_hz + s.doppler_rate_hz_s * tm
    return measure_oscillator(src.read, fs, predict, range(1, 33), seconds, progress=progress), 40000000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8001")
    ap.add_argument("--host", default="192.168.3.1")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--alt", type=float, default=0.0)
    ap.add_argument("--fs", type=float, default=4e6)
    ap.add_argument("--rx-seconds", type=float, default=30.0)
    ap.add_argument("--rx-gain", type=float, default=60.0)
    ap.add_argument("--tx-gain", type=float, default=-40.0)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--osc-error", type=float, help="skip the measurement, use this osc_error_ppm")
    ap.add_argument("--no-rx", action="store_true",
                    help="skip the measurement and predict the start error from the thermal state left by the last run")
    ap.add_argument("--holdover", default=None, help="holdover state file")
    ap.add_argument("--dry", action="store_true", help="print the request and stop")
    ap.add_argument("--demo", action="store_true", help="synthetic IQ instead of the board RX (status marked demo)")
    ap.add_argument("--demo-y", type=float, default=-3.5228e-6, help="oscillator error of the synthetic IQ")
    ap.add_argument("--mqtt-host", default=None, help="MQTT broker for the Dashboard (default: --host)")
    ap.add_argument("--no-mqtt", action="store_true")
    args = ap.parse_args()

    ho_path = args.holdover or DEFAULT_PATH
    ho = Holdover.load(ho_path)
    st = StatusPublisher(None if args.no_mqtt else (args.mqtt_host or args.host), demo=args.demo)
    st.update(True, phase="idle", msg="", hist=[], svs=[], acq=[], fix=None, tx=None, osc=None,
              rx={"fs": args.fs, "gain_db": args.rx_gain, "t": 0.0, "seconds": args.rx_seconds})
    try:
        xo_hz = None
        sigma_ppb = None
        if args.no_rx and args.osc_error is None:
            fix_ppm, sigma_ppb = ho.estimate_start(time.time())
            print(f"no RX: start error predicted {fix_ppm:+.6f} ppm +-{sigma_ppb:.1f} ppb")
        elif args.osc_error is None:
            st.update(True, phase="acquiring", msg="searching satellites")
            prog = st.rx_progress(args.fs, args.rx_seconds, args.rx_gain)
            if args.demo:
                m, xo_hz = demo_measure(args.fs, args.rx_seconds, args.demo_y, prog)
            else:
                m, xo_hz = measure(args.host, args.lat, args.lon, args.alt, args.rx_seconds, args.fs,
                                   args.rx_gain, progress=prog)
            if m is None:
                msg = "no clock fix: no GNSS satellites acquired (antenna powered? sky view?)"
                st.update(True, phase="error", msg=msg)
                print(msg)
                sys.exit(2)
            fix_ppm = total_error_ppm(m.y, xo_hz)
            print(f"measured y {m.y * 1e9:+.3f} ppb on {len(m.acquired)} SVs -> osc error {fix_ppm:+.6f} ppm")
        else:
            fix_ppm = args.osc_error
        if sigma_ppb is not None:
            src = f"PREDICTED from the previous run, no measurement: +-{sigma_ppb:.0f} ppb = +-{sigma_ppb * L1_HZ_PER_PPB:.0f} Hz at L1"
        else:
            src = "measured by the GPSDO" if args.osc_error is None else "entered manually"
        st.log("measure", f"start error e0 = {fix_ppm:+.6f} ppm ({src})")
        # the board idles between the measurement and the start, so the error is not carried forward
        rate, accel = ho.ramp(fix_ppm, args.duration)
        dec = decision(ho, fix_ppm, rate, accel, args.duration, args.lat)
        for kind, text in dec.pop("_log"):
            st.log(kind, text)
        ts = list(range(0, int(args.duration) + 1, 5))
        pred = [[t, round(float(v), 3)] for t, v in zip(ts, ho.error_ppb(fix_ppm, ts))]
        osc = {"xo_hz": dec["xo_hz"] if xo_hz is None else xo_hz,
               "xo_ppm": round((dec["xo_hz"] / 40e6 - 1) * 1e6, 6),
               "error_ppm": round(fix_ppm, 6), "sigma_ppb": None if sigma_ppb is None else round(sigma_ppb, 2),
               "source": "predicted" if sigma_ppb is not None else ("measured" if args.osc_error is None else "manual")}
        tx = {"active": False, "t": 0.0, "duration": args.duration, "rate_ppb_s": round(rate, 5),
              "accel_ppb_s2": round(accel, 9), "asymptote_ppm": ho.asymptote_ppm, "tau_s": ho.tau_heat_s,
              "pred": pred}
        st.update(True, phase="measured", msg="", osc=osc, tx=tx, decision=dec)
        body = build_body(args, fix_ppm, rate, accel)
        print("live start:", json.dumps(body))
        if args.dry or args.demo:
            return
        req = urllib.request.Request(args.url + "/api/live/start", data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"})
        t_tx = time.time()
        st.update(True, phase="tx", tx={**tx, "active": True, "t": 0.0})
        st.log("tx", "transmission started with the commanded ramp")
        stop = threading.Event()

        def tick():
            last_log = 0
            while not stop.wait(1.0):
                t = time.time() - t_tx
                st.update(tx={**tx, "active": True, "t": round(t, 1)})
                if t - last_log >= 30.0:
                    last_log = t
                    eps = float(ho.error_ppb(fix_ppm, [t])[0])
                    st.log("tx", f"t = {t:.0f} s: predicted change {eps:+.2f} ppb "
                                 f"(carrier {eps * L1_HZ_PER_PPB:+.2f} Hz at L1)")
        threading.Thread(target=tick, daemon=True).start()
        try:
            with urllib.request.urlopen(req) as resp:
                for line in resp:
                    if line.startswith(b"data:") and b'"finished"' in line:
                        break
        finally:
            stop.set()
        ho.mark_end(time.time(), fix_ppm, time.time() - t_tx)
        ho.save(ho_path)
        st.update(True, phase="done", tx={**tx, "active": False, "t": round(time.time() - t_tx, 1)})
        st.log("tx", f"transmission finished after {time.time() - t_tx:.0f} s")
        print("transmission done")
    finally:
        st.close()


if __name__ == "__main__":
    main()
