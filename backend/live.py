from __future__ import annotations

import copy
import threading
from dataclasses import dataclass

import numpy as np

from backend import generator, geometry, inspector, scenario

_ENU_DIRECTIONS = {
    "north": (0.0, 1.0, 0.0), "south": (0.0, -1.0, 0.0),
    "east": (1.0, 0.0, 0.0), "west": (-1.0, 0.0, 0.0),
    "up": (0.0, 0.0, 1.0), "down": (0.0, 0.0, -1.0),
}
# Only one time control is real: a GPS-time-of-week shift applied to the
# whole segment (start + nav epochs move together, see
# generator.run_segment). A separate PPS-phase / satellite-clock knob
# would need its own distinct effect on the generated signal; until one
# exists, nothing here pretends to offer it.
_TIME_FIELDS = {"time_offset_s"}


@dataclass
class LiveState:
    llh: list[float]
    time_offset_s: float = 0.0


class LiveSession:
    def __init__(self, base_req: scenario.ScenarioRequest, segment_duration_s: float = 1.0):
        self.base_req = base_req
        self.segment_duration_s = segment_duration_s
        self.state = LiveState(llh=[base_req.lat, base_req.lon, base_req.alt])
        self._lock = threading.Lock()
        self.running = False
        self.consecutive_errors = 0

    def jog(self, direction: str, distance_m: float) -> None:
        if direction not in _ENU_DIRECTIONS:
            raise ValueError(f"unknown jog direction {direction!r}")
        e, n, u = _ENU_DIRECTIONS[direction]
        with self._lock:
            rx = np.array(geometry.llh_to_ecef(*self.state.llh))
            e_hat, n_hat, u_hat = geometry._enu(rx)
            delta = distance_m * (e * e_hat + n * n_hat + u * u_hat)
            new_ecef = rx + delta
            lat, lon, h = _ecef_to_llh(*new_ecef)
            self.state.llh = [lat, lon, h]

    def shift_time(self, field: str, delta: float) -> None:
        if field not in _TIME_FIELDS:
            raise ValueError(f"unknown time field {field!r}")
        with self._lock:
            setattr(self.state, field, getattr(self.state, field) + delta)

    def segments(self):
        """Generator of complex IQ chunks -- transmit.stream()'s chunk_source
        for a live session. Stops (StopIteration) once self.running is set
        False by the caller (mirrors TxSession's cancel-event pattern but
        drives generation, not just playback)."""
        self.running = True
        while self.running:
            with self._lock:
                snap = copy.deepcopy(self.state)
            try:
                outdir = generator.run_segment(
                    self.base_req, llh=tuple(snap.llh),
                    time_offset_s=snap.time_offset_s,
                    duration_s=self.segment_duration_s)
                iq = inspector.read_iq(outdir / "gpssim.bin", self.base_req.sample_format)
                self.consecutive_errors = 0
                yield iq
            except Exception:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.running = False
                    raise

    def stop(self) -> None:
        self.running = False


def _ecef_to_llh(x, y, z):
    # Same iterative WGS84 inverse as receiver.py:_ecef_to_llh -- duplicated
    # here (not imported) because receiver.py's version is private (leading
    # underscore) and this module has no other dependency on receiver.py;
    # promoting it to geometry.py is out of scope for this task.
    a, e2 = 6378137.0, 6.69437999014e-3
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(6):
        nrad = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - nrad
        lat = np.arctan2(z, p * (1 - e2 * nrad / (nrad + h)))
    return float(np.degrees(lat)), float(np.degrees(lon)), float(h)
