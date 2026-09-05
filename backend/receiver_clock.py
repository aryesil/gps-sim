"""Deterministic receiver-clock error model.

Disabled by default. When enabled it produces the receiver's own clock
offset as a function of time -- a slow polynomial (bias + drift + drift
rate) with an optional sawtooth to mimic a steered/disciplined
oscillator. It is fully deterministic: the same config and the same
epoch always give the same offset, no RNG.

Three distinct quantities must not be confused:

* satellite clock error   -- from the broadcast af0/af1/af2 (+ the
  relativistic term); lives in ``geometry.sat_state``/``reference``.
* propagation delay        -- geometric range / c, plus the optional
  atmospheric delay in ``backend.atmosphere``.
* receiver clock offset    -- THIS module. It shifts the receiver's idea
  of "now", so it adds a common range-equivalent term ``c * offset`` to
  every simultaneously-observed pseudorange and advances the sampling
  clock. It does not touch geometry or Doppler-from-motion; a non-zero
  drift does add a common carrier-frequency offset ``-f_L1 * drift``.

Nothing here is wired into ``geometry`` -- callers opt in and apply the
offset once, the same way they do for ``atmosphere``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_C = 299792458.0
_L1_HZ = 1_575_420_000.0


@dataclass
class ReceiverClockConfig:
    model: str = "off"                 # "off" | "poly"
    bias_s: float = 0.0                # offset at t = ref_epoch_s
    drift_s_per_s: float = 0.0         # linear term (a.k.a. fractional freq error)
    drift_rate_s_per_s2: float = 0.0   # quadratic term
    ref_epoch_s: float = 0.0           # GPS SoW the polynomial is expanded about
    # optional sawtooth of a steered oscillator: reset by `sawtooth_amp_s`
    # every `sawtooth_period_s` once |accumulated drift| would exceed it
    sawtooth_amp_s: float = 0.0
    sawtooth_period_s: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.model != "off"

    @classmethod
    def from_dict(cls, d: dict | None) -> "ReceiverClockConfig":
        if not d:
            return cls()
        f = {k: d[k] for k in (
            "model", "bias_s", "drift_s_per_s", "drift_rate_s_per_s2",
            "ref_epoch_s", "sawtooth_amp_s", "sawtooth_period_s") if k in d}
        cfg = cls(**f)
        if cfg.model not in {"off", "poly"}:
            raise ValueError(f"receiver_clock.model: unknown model {cfg.model!r}")
        if cfg.sawtooth_amp_s < 0 or cfg.sawtooth_period_s < 0:
            raise ValueError("receiver_clock: sawtooth parameters must be >= 0")
        return cfg


def offset_s(cfg: ReceiverClockConfig, gps_sow: float) -> float:
    """Receiver clock offset (s) at ``gps_sow``. Positive == receiver
    clock reads ahead of true GPS time. Deterministic."""
    if not cfg.enabled:
        return 0.0
    t = gps_sow - cfg.ref_epoch_s
    off = (cfg.bias_s
           + cfg.drift_s_per_s * t
           + 0.5 * cfg.drift_rate_s_per_s2 * t * t)
    if cfg.sawtooth_amp_s > 0.0 and cfg.sawtooth_period_s > 0.0:
        # subtract a staircase so the steered offset stays within +/- amp
        phase = math.floor(t / cfg.sawtooth_period_s)
        off -= phase * cfg.sawtooth_amp_s
    return off


def state(cfg: ReceiverClockConfig, gps_sow: float) -> dict:
    """Full breakdown for one epoch.

    ``range_bias_m``     add to every pseudorange observed at this epoch.
    ``carrier_offset_hz`` common carrier-frequency offset from the drift.
    ``clock_offset_s``   the receiver clock offset itself.
    """
    off = offset_s(cfg, gps_sow)
    return {
        "model": cfg.model,
        "clock_offset_s": off,
        "range_bias_m": _C * off,
        "drift_s_per_s": cfg.drift_s_per_s if cfg.enabled else 0.0,
        "carrier_offset_hz": (-_L1_HZ * cfg.drift_s_per_s) if cfg.enabled else 0.0,
    }
