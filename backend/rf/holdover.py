"""Predicted oscillator drift for the time the board transmits (holdover).

While the board transmits it cannot receive, so the oscillator error measured before the
transmission (``e0``, from the GPSDO measurement) has to be carried through the transmission.
Transmitting heats the board and the VCTCXO error relaxes towards a settled value ``A``:

    e(t) = e0 + (A - e0) * (1 - exp(-t / tau))

``A`` is the oscillator error of a board that has been transmitting for a long time. A 10 minute
run that started cool (-3.52 ppm) fits A -3.564 ppm with tau 750 s on its own, but a run that
started hot (-3.570 ppm, a few minutes after an earlier transmission) keeps falling at
-0.026 ppb/s, so A lies below -3.57 ppm (the hot board starts below the old A and a model that
relaxes towards it has the wrong sign). The joint fit of both runs (shared A and tau, own e0 per
run, 0.48 ppb rms) is A -3.594 ppm, tau 1500 s; longer runs are needed to pin the two apart.
The start error ``e0`` is always measured,
never predicted, so the model needs no cool-down history: the thermal state is already inside
``e0``. The command generator takes a quadratic ramp, which is the exponential expanded to second
order and good for roughly the first ``tau / 2``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

DEFAULT_PATH = Path.home() / ".gps-sim" / "holdover.json"


@dataclass
class Holdover:
    asymptote_ppm: float = -3.594      # oscillator error of a board settled in transmission
    tau_heat_s: float = 1500.0         # settling time while transmitting
    cool_ppm: float = -3.5246          # oscillator error of a board that has idled for a long time
    tau_cool_s: float = 4000.0         # relaxation time towards ``cool_ppm`` while idle (provisional, one measured pair)
    last_end_unix: float = 0.0         # when the last transmission ended (0: unknown)
    last_end_ppm: float = 0.0          # its predicted oscillator error at that moment

    def ramp(self, e0_ppm: float, duration_s: float | None = None) -> tuple[float, float]:
        """``(osc_drift_ppb_per_s, osc_drift_accel_ppb_per_s2)`` for a transmission that begins with
        the measured oscillator error ``e0_ppm``. Without ``duration_s`` this is the exponential's
        expansion at the start; with it, the quadratic through zero that fits the exponential best
        (least squares) over the planned run, which keeps the error small to the end of the run."""
        d = (self.asymptote_ppm - e0_ppm) * 1e3                  # ppb still to travel
        if not duration_s or duration_s <= 0.0:
            return d / self.tau_heat_s, -d / self.tau_heat_s ** 2
        t = np.linspace(0.0, float(duration_s), 241)
        A = np.column_stack([t, 0.5 * t * t])
        (rate, accel), *_ = np.linalg.lstsq(A, self.error_ppb(e0_ppm, t), rcond=None)
        return float(rate), float(accel)

    def error_ppb(self, e0_ppm: float, t_s) -> np.ndarray:
        """Predicted error change since the start, in ppb."""
        d = (self.asymptote_ppm - e0_ppm) * 1e3
        return d * (1.0 - np.exp(-np.asarray(t_s, float) / self.tau_heat_s))

    def estimate_start(self, now_unix: float) -> tuple[float, float]:
        """``(e0_ppm, sigma_ppb)`` for a transmission that starts at ``now_unix`` when the start error
        could not be measured (no RX, no sky). The board's thermal state is carried from the end of
        the previous transmission (``mark_end``) and relaxes towards the cool value while idle. With
        no history the board is assumed cool, but it may be hot, so the uncertainty is the whole
        span between the cool and the settled error. ``sigma_ppb`` is a rough 1-sigma: a few ppb of
        model error plus a fifth of the excursion still left from the cool value."""
        span_ppb = abs(self.cool_ppm - self.asymptote_ppm) * 1e3
        if self.last_end_unix <= 0.0 or now_unix < self.last_end_unix:
            return self.cool_ppm, 0.5 * span_ppb
        idle = now_unix - self.last_end_unix
        e = self.cool_ppm + (self.last_end_ppm - self.cool_ppm) * float(np.exp(-idle / self.tau_cool_s))
        return e, 3.0 + 0.2 * abs(e - self.cool_ppm) * 1e3

    def mark_end(self, end_unix: float, e0_ppm: float, duration_s: float) -> None:
        """Record the end of a transmission that started with error ``e0_ppm`` and ran ``duration_s``
        (the state ``estimate_start`` carries to the next run)."""
        self.last_end_unix = float(end_unix)
        self.last_end_ppm = float(e0_ppm + self.error_ppb(e0_ppm, [duration_s])[0] * 1e-3)

    def learn(self, t_s, err_ppb, applied_ppm: float, taus=range(300, 2001, 50),
              applied_rate_ppb_s: float = 0.0, applied_accel_ppb_s2: float = 0.0) -> None:
        """Refit ``asymptote_ppm`` and ``tau_heat_s`` from a transmission: ``err_ppb`` is the on-air
        common offset against the truth at ``t_s`` seconds (+ = carrier high) and ``applied_ppm``
        the ``osc_error_ppm`` the run used, with the ramp it was commanded
        (``applied_rate_ppb_s``, ``applied_accel_ppb_s2``) which is added back so that the fit sees
        the oscillator's own movement. Model: ``c + d * (1 - exp(-t / tau))``; the board's
        real start error is ``applied + c`` and the asymptote ``applied + c + d``."""
        t = np.asarray(t_s, float)
        y = np.asarray(err_ppb, float) + applied_rate_ppb_s * t + 0.5 * applied_accel_ppb_s2 * t * t
        best = None
        for tau in taus:
            A = np.column_stack([np.ones_like(t), 1.0 - np.exp(-t / tau)])
            (c, d), *_ = np.linalg.lstsq(A, y, rcond=None)
            r = float(np.sum((A @ [c, d] - y) ** 2))
            if best is None or r < best[0]:
                best = (r, float(tau), float(c), float(d))
        _, tau, c, d = best
        self.tau_heat_s = tau
        self.asymptote_ppm = applied_ppm + (c + d) * 1e-3

    def save(self, path: Path | str = DEFAULT_PATH) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=1), encoding="utf8")

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> "Holdover":
        try:
            d = json.loads(Path(path).read_text(encoding="utf8"))
            return cls(**{k: float(v) for k, v in d.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return cls()
