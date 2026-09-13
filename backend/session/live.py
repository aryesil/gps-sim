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

# Every RF-band output file the native/gps-sdr-sim engines can produce
# (backend.synth.bands.BAND_REGISTRY's out_file values, GLONASS's G1/G2
# included even though they aren't one of the user-facing "L1"/"L2"/"L5"
# band choices -- GLONASS rides its own file whenever it's in `systems`).
# A live TX stream feeds exactly one physical output, so exactly one of
# these may exist in a segment's outdir; see _resolve_band_file.
_BAND_FILES = ("gpssim.bin", "gpssim_g1.bin", "gpssim_g2.bin",
               "gpssim_l2.bin", "gpssim_l5.bin")


def _resolve_band_file(outdir) -> str:
    """Which of ``_BAND_FILES`` this segment actually produced.

    A live session's engine/systems/bands are fixed for its whole run (only
    position and time are live-adjustable), so this only needs to run once
    -- LiveSession caches the result after the first segment. Zero matches
    means the request selected a system RINEX has no records for; more than
    one means the systems/bands combination would need more than one
    physical RF output (e.g. GPS L1 + GLONASS, which sits on its own G1
    band) -- live transmit has exactly one output per channel, so that is
    rejected here rather than silently streaming just one of the bands."""
    present = [f for f in _BAND_FILES if (outdir / f).exists()]
    if not present:
        raise RuntimeError(
            "live segment produced no known IQ output file -- check that "
            "`systems`/`bands` actually have RINEX coverage for this run")
    if len(present) > 1:
        raise RuntimeError(
            "live transmit supports exactly one RF output per channel, but "
            f"this systems/bands selection produced {len(present)}: "
            f"{present!r} -- pick a single band (or drop an overlapping "
            "system, e.g. GLONASS alongside GPS on the same band) for Start")
    return present[0]


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
        self._iq_filename: str | None = None   # resolved from the first segment

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
                if self._iq_filename is None:
                    # A systems/bands combination needing >1 physical output
                    # is a static config error, not a transient generation
                    # hiccup -- surface it immediately rather than burning
                    # through the retry budget below on a request that can
                    # never succeed.
                    self._iq_filename = _resolve_band_file(outdir)
                iq = inspector.read_iq(outdir / self._iq_filename, self.base_req.sample_format)
                self.consecutive_errors = 0
                yield iq
            except RuntimeError:
                self.running = False
                raise
            except Exception:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.running = False
                    raise

    def stop(self) -> None:
        self.running = False

    def snapshot(self) -> dict:
        """Current segment anchor: receiver ECEF and the GPS-time-of-week
        offset applied to the next segment. Used to check that successive
        live segments join without a position or timing gap."""
        with self._lock:
            ecef = geometry.llh_to_ecef(*self.state.llh)
            return {"ecef": list(ecef), "llh": list(self.state.llh),
                    "time_offset_s": float(self.state.time_offset_s)}


def segment_boundary_gap(prev: dict, cur: dict) -> dict:
    """Discontinuity between two consecutive live-segment anchors.

    ``position_gap_m`` -- how far the receiver jumped between segments.
    ``time_gap_s``     -- change in the GPS-time-of-week offset.
    A caller enforces its own per-tick limits (e.g. a jog step cap and a
    max time nudge); this only measures."""
    p0 = np.array(prev["ecef"], float)
    p1 = np.array(cur["ecef"], float)
    return {
        "position_gap_m": float(np.linalg.norm(p1 - p0)),
        "time_gap_s": float(cur["time_offset_s"] - prev["time_offset_s"]),
    }


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
