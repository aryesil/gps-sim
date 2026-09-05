"""Canonical scenario truth model.

One authoritative object that turns a scenario request (geodetic
position + UTC start + duration) into every derived quantity the
generator and the validator need, through a single conversion path:

* geodetic -> ECEF        via ``geometry.llh_to_ecef`` (WGS84)
* UTC       -> GPS time   via ``gpstime.utc_to_gps`` (leap seconds)
* GPS SoW passed to ephemeris propagation is the fixture-consistent
  seconds-of-week (the +/- half-week unwrap in ``geometry.sat_state``
  absorbs a week rollover within the run).

The point is that both sides of a validation agree on *inputs*. They
must still disagree on *implementation*: the validator cross-checks
geometry with ``backend.reference`` (an independent IS-GPS-200
propagator), never by calling the same code the generator used.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt

import numpy as np

from backend import geometry, gpstime


@dataclass(frozen=True)
class ScenarioTruth:
    lat_deg: float
    lon_deg: float
    alt_m: float
    start_utc: dt.datetime
    duration_s: float

    # --- canonical conversions (one path, computed on demand) ----------
    @property
    def rx_ecef(self) -> tuple[float, float, float]:
        return geometry.llh_to_ecef(self.lat_deg, self.lon_deg, self.alt_m)

    @property
    def start_gps(self) -> gpstime.GPSTime:
        return gpstime.utc_to_gps(self.start_utc)

    @property
    def gps_week(self) -> int:
        return self.start_gps.week

    @property
    def start_sow(self) -> float:
        return self.start_gps.sow

    def sow_at(self, offset_s: float) -> float:
        """GPS seconds-of-week ``offset_s`` into the run. Continuous
        across a week rollover in absolute seconds; callers that feed
        ephemeris propagation want this SoW value directly."""
        return self.start_gps.shifted(offset_s).sow

    # --- constellation truth -----------------------------------------
    def observables(self, eph_by_prn: dict, offset_s: float = 0.0,
                    mask_deg: float = 5.0, rx_vel=(0.0, 0.0, 0.0)) -> list[dict]:
        """Truth observables for every PRN above the mask at ``offset_s``.
        ``eph_by_prn`` values may be broadcast dicts or state_fns."""
        sow = self.sow_at(offset_s)
        rx = self.rx_ecef
        out = []
        for prn in sorted(eph_by_prn):
            o = geometry.observables(eph_by_prn[prn], rx, sow, rx_vel=rx_vel)
            if o["el_deg"] >= mask_deg:
                o["prn"] = prn
                out.append(o)
        return out

    def sat_positions(self, eph_by_prn: dict, offset_s: float = 0.0) -> dict:
        sow = self.sow_at(offset_s)
        rx = np.array(self.rx_ecef)
        out = {}
        for prn, eph in eph_by_prn.items():
            pos, _, _, _ = geometry.solve_transmit_time(eph, rx, sow)
            out[prn] = pos
        return out

    def as_dict(self) -> dict:
        return {
            "lat_deg": self.lat_deg, "lon_deg": self.lon_deg, "alt_m": self.alt_m,
            "start_utc": self.start_utc.isoformat(),
            "duration_s": self.duration_s,
            "rx_ecef": list(self.rx_ecef),
            "gps_week": self.gps_week,
            "start_sow": self.start_sow,
        }


def from_request(req) -> ScenarioTruth:
    """Build the truth model from a ``scenario.ScenarioRequest`` (or any
    object with lat/lon/alt/start/duration_s)."""
    return ScenarioTruth(
        lat_deg=float(req.lat), lon_deg=float(req.lon), alt_m=float(req.alt),
        start_utc=req.start, duration_s=float(req.duration_s))
