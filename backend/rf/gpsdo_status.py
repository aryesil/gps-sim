# SPDX-License-Identifier: MIT
"""Live GPSDO status for the Tezuka Dashboard.

The measurement and the transmission run on the PC; the Dashboard reads the board's Mosquitto
broker. ``StatusPublisher`` keeps one JSON document (retained, topic ``state/system/gpsdo/status``)
and republishes it at most twice a second. The MQTT client is a minimal MQTT 3.1.1 publisher
(QoS 0), so there is no extra dependency.

Document layout (all numbers plain JSON numbers, errors in ppb unless the key says ppm):

    phase    idle | acquiring | tracking | measured | tx | done | error
    demo     true when the IQ is synthetic (no real GNSS signal)
    rx       fs, gain_db, t (s into the capture), seconds
    acq      [{prn, metric, doppler_hz, tracked}] best candidates of the acquisition
    svs      [{prn, cn0, doppler_hz, locked, y_ppb, used}] tracked satellites
    fix      {y_ppb, sigma_ppb, nsv, spread_ppb, windows}: running mean of the windows
    hist     [{t, y_ppb, sigma_ppb, nsv}] one entry per window (last 300)
    osc      {xo_hz, xo_ppm, error_ppm}: what the TX gets (driver xo_correction, measured error)
    tx       {active, t, duration, rate_ppb_s, accel_ppb_s2, asymptote_ppm, tau_s, pred: [[t, ppb]]}
    decision what the holdover model decided from the measurement (see ``decision`` in gpsdo_cycle)
    log      [{ts, kind, text}] the decisions in order (last 60); kind: measure | model | apply | tx | warn
"""
from __future__ import annotations

import json
import socket
import threading
import time

import numpy as np

TOPIC = "state/system/gpsdo/status"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _utf8(s: str) -> bytes:
    b = s.encode()
    return len(b).to_bytes(2, "big") + b


class MqttPublisher:
    """Fire-and-forget retained publisher; reconnects on the next publish after an error."""

    def __init__(self, host: str, port: int = 1883, client_id: str = "gps-sim-gpsdo"):
        self.host, self.port, self.client_id = host, port, client_id
        self._sock = None

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=4.0)
        body = _utf8("MQTT") + bytes([4, 0x02]) + (0).to_bytes(2, "big") + _utf8(self.client_id)
        s.sendall(bytes([0x10]) + _varint(len(body)) + body)
        ack = s.recv(4)
        if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
            s.close()
            raise ConnectionError(f"MQTT connect refused: {ack!r}")
        self._sock = s

    def publish(self, topic: str, payload: str, retain: bool = True) -> bool:
        body = _utf8(topic) + payload.encode()
        pkt = bytes([0x30 | (1 if retain else 0)]) + _varint(len(body)) + body
        for _ in range(2):
            try:
                if self._sock is None:
                    self._connect()
                self._sock.sendall(pkt)
                return True
            except OSError:
                self.close()
        return False

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class StatusPublisher:
    """Holds the status document; ``update`` merges sections and publishes (rate limited)."""

    HIST = 300

    def __init__(self, host: str | None, demo: bool = False, min_interval_s: float = 0.5):
        self.pub = MqttPublisher(host) if host else None
        self.min_interval = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()
        self.doc = {"v": 1, "phase": "idle", "msg": "", "demo": demo, "hist": [], "svs": [], "acq": [], "log": []}

    def update(self, force: bool = False, **sections):
        with self._lock:
            self.doc.update(sections)
            self.doc["ts"] = round(time.time(), 2)
            now = time.monotonic()
            if not force and now - self._last < self.min_interval:
                return
            self._last = now
            text = json.dumps(self.doc, separators=(",", ":"), allow_nan=False)
        if self.pub is not None:
            self.pub.publish(TOPIC, text)

    def close(self):
        if self.pub is not None:
            self.pub.close()

    def log(self, kind: str, text: str, **sections):
        """Append a decision to the log (and merge ``sections``); always published."""
        entry = {"ts": round(time.time(), 2), "kind": kind, "text": text}
        self.update(True, log=(self.doc.get("log", []) + [entry])[-60:], **sections)

    # -- adapters ---------------------------------------------------------------------------
    def rx_progress(self, fs: float, seconds: float, gain_db: float):
        """``progress`` callback for ``gnss_rx.measure.measure_oscillator``."""
        windows = []

        def sv_rows(trackers, fix):
            rows = []
            for prn, tr in sorted(trackers.items()):
                ep = tr.epochs[-1] if tr.epochs else None
                rows.append({"prn": prn, "cn0": round(ep.cn0_dbhz, 1) if ep else None,
                             "doppler_hz": round(float(tr._f), 1), "locked": bool(ep.locked) if ep else False,
                             "y_ppb": round(fix.per_sv[prn] * 1e9, 2) if fix and prn in fix.per_sv else None,
                             "used": bool(fix and prn in fix.used)})
            return rows

        def cb(event, **kw):
            rx = {"fs": fs, "gain_db": gain_db, "seconds": seconds}
            if event == "acquired":
                found = sorted(kw["found"].items(), key=lambda kv: -kv[1][0])
                acq = [{"prn": p, "metric": round(m, 2), "doppler_hz": round(d, 0), "tracked": bool(t)}
                       for p, (m, d, t) in found[:12]]
                self.update(True, phase="tracking" if any(a["tracked"] for a in acq) else "error",
                            msg="" if any(a["tracked"] for a in acq) else "no satellites acquired",
                            acq=acq, rx={**rx, "t": 0.0})
            elif event == "window":
                fix = kw["fix"]
                windows.append((kw["t"], fix))
                ys = np.array([f.y for _, f in windows]) * 1e9
                spread = float(ys.std(ddof=1)) if len(ys) > 1 else 0.0
                hist = self.doc["hist"] + [{"t": round(kw["t"], 1), "y_ppb": round(fix.y * 1e9, 3),
                                           "sigma_ppb": round(fix.sigma * 1e9, 3), "nsv": len(fix.used)}]
                self.update(hist=hist[-self.HIST:], svs=sv_rows(kw["trackers"], fix),
                            fix={"y_ppb": round(float(ys.mean()), 3), "sigma_ppb": round(spread / np.sqrt(len(ys)), 3) if len(ys) > 1 else None,
                                 "nsv": len(fix.used), "spread_ppb": round(spread, 3), "windows": len(ys),
                                 "last_ppb": round(fix.y * 1e9, 3)})
            elif event == "tracking":
                last = windows[-1][1] if windows else None
                self.update(rx={**rx, "t": round(kw["t"], 1)}, svs=sv_rows(kw["trackers"], last))
        return cb
