"""Deterministic specular-multipath channel model.

Disabled by default. When enabled it describes the received signal for
one line of sight as the direct ray plus a small number of specular
reflections, each with its own excess delay, amplitude (relative to the
direct ray), carrier phase and Doppler offset. Everything is a fixed
parameter -- no RNG -- so a scenario replays bit-for-bit.

Two products:

* ``channel_taps`` -- the complex baseband channel
  ``h(0) = 1`` (direct) plus one tap per reflection at its excess delay,
  suitable for convolving the clean per-PRN signal.
* ``tracking_bias`` -- the code- and carrier-phase error an early-late
  DLL / Costas loop would settle to for that channel, as a closed-form
  approximation (single-reflection formula applied per reflection and
  summed). This is what a metadata-level pseudorange model should add;
  it is NOT a substitute for actually filtering the IQ.

Nothing here is wired into ``geometry``; callers opt in.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

_C = 299792458.0
_CA_CHIP_HZ = 1_023_000.0
_L1_HZ = 1_575_420_000.0
_CHIP_M = _C / _CA_CHIP_HZ            # ~293.05 m


@dataclass
class Reflection:
    excess_delay_m: float             # extra path length vs the direct ray, metres (>0)
    amplitude: float                  # relative to the direct ray (0..1 typical)
    phase_rad: float = math.pi        # carrier phase offset (pi == ground bounce)
    doppler_hz: float = 0.0           # carrier Doppler offset vs the direct ray


@dataclass
class MultipathConfig:
    model: str = "off"                # "off" | "specular"
    reflections: list = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.model != "off" and bool(self.reflections)

    @classmethod
    def from_dict(cls, d: dict | None) -> "MultipathConfig":
        if not d:
            return cls()
        model = d.get("model", "off")
        if model not in {"off", "specular"}:
            raise ValueError(f"multipath.model: unknown model {model!r}")
        refs = []
        for r in d.get("reflections", []):
            refs.append(Reflection(
                excess_delay_m=float(r["excess_delay_m"]),
                amplitude=float(r["amplitude"]),
                phase_rad=float(r.get("phase_rad", math.pi)),
                doppler_hz=float(r.get("doppler_hz", 0.0)),
            ))
        for r in refs:
            if r.excess_delay_m < 0:
                raise ValueError("multipath: excess_delay_m must be >= 0")
            if not (0.0 <= r.amplitude < 1.0):
                raise ValueError("multipath: amplitude must be in [0, 1)")
        return cls(model=model, reflections=refs)


def channel_taps(cfg: MultipathConfig, t_s: float = 0.0) -> list[tuple[float, complex]]:
    """``[(delay_s, complex_gain), ...]`` with the direct ray first
    (delay 0, gain 1). Reflection gains rotate with their Doppler, so the
    channel is time-varying when ``doppler_hz != 0``."""
    taps = [(0.0, 1.0 + 0.0j)]
    if not cfg.enabled:
        return taps
    for r in cfg.reflections:
        ph = r.phase_rad + 2.0 * math.pi * r.doppler_hz * t_s
        taps.append((r.excess_delay_m / _C, r.amplitude * complex(math.cos(ph), math.sin(ph))))
    return taps


def _single_reflection_bias(a: float, tau_m: float, phi: float) -> tuple[float, float]:
    """Closed-form early-late DLL / Costas settling error for one
    reflection of relative amplitude ``a``, excess delay ``tau_m`` (m),
    carrier phase ``phi``. Returns ``(code_bias_m, carrier_bias_rad)``.

    Standard narrow-correlator approximation, valid for tau below ~1.5
    chips; beyond that the reflection is largely uncorrelated and the
    bias rolls off, which the ``min`` below mimics crudely.
    """
    if a == 0.0 or tau_m <= 0.0:
        return 0.0, 0.0
    c = math.cos(phi)
    s = math.sin(phi)
    rolloff = 1.0 if tau_m <= _CHIP_M else max(0.0, 2.0 - tau_m / _CHIP_M)
    # carrier phase pull-in
    carrier_bias = math.atan2(a * s * rolloff, 1.0 + a * c * rolloff)
    # code discriminator zero shifts toward the reflection, scaled by the
    # in-phase component and the fraction of a chip the echo sits at
    frac = min(tau_m / _CHIP_M, 1.0)
    code_bias = (a * c * rolloff) / (1.0 + a * c * rolloff) * frac * _CHIP_M * 0.5
    return code_bias, carrier_bias


def tracking_bias(cfg: MultipathConfig, t_s: float = 0.0) -> dict:
    """Approximate steady-state tracking error for the whole channel.

    ``code_bias_m``    add to the pseudorange / code phase.
    ``carrier_bias_rad`` carrier-phase pull (also given as metres of L1).
    """
    code = 0.0
    carr = 0.0
    if cfg.enabled:
        for r in cfg.reflections:
            phi = r.phase_rad + 2.0 * math.pi * r.doppler_hz * t_s
            dc, d_carr = _single_reflection_bias(r.amplitude, r.excess_delay_m, phi)
            code += dc
            carr += d_carr
    return {
        "model": cfg.model,
        "n_reflections": len(cfg.reflections) if cfg.enabled else 0,
        "code_bias_m": code,
        "carrier_bias_rad": carr,
        "carrier_bias_m": carr / (2.0 * math.pi) * (_C / _L1_HZ),
    }
