"""Deterministic per-SV fading configuration and truth-side preview.

The fading *gain model* is a published model shared with synthesis on purpose:
``predicted_metric_db`` calls the same C ``fading_gain_linear`` the native
engine applies, so the preview matches what the engine bakes into the IQ.
Acquisition parity still measures the generated IQ independently.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

_MODELS = {"off", "lognormal"}


@dataclass(frozen=True)
class FadingConfig:
    model: str = "off"
    sigma_db: float = 0.0
    coherence_s: float = 1.0
    seed: int = 0

    @staticmethod
    def from_dict(d: dict | None) -> "FadingConfig":
        d = d or {}
        model = d.get("model", "off")
        if model not in _MODELS:
            raise ValueError(f"fading.model must be one of {_MODELS}")
        sigma = float(d.get("sigma_db", 0.0))
        coh = float(d.get("coherence_s", 1.0))
        if sigma < 0:
            raise ValueError("fading.sigma_db must be >= 0")
        if coh <= 0:
            raise ValueError("fading.coherence_s must be > 0")
        return FadingConfig(model, sigma, coh, int(d.get("seed", 0)))

    def enabled(self) -> bool:
        return self.model != "off" and self.sigma_db > 0.0


def predicted_metric_db(prn: int, t_s: float, cfg: FadingConfig,
                        base_metric_db: float) -> float:
    """Shift a nominal acquisition metric by the same per-SV fading gain the
    synthesis applies at ``t_s``, so the preview tracks generation. Returns
    ``base_metric_db`` unchanged when fading is disabled."""
    if not cfg.enabled():
        return base_metric_db
    return base_metric_db + _gain_db(prn, t_s, cfg)


def _gain_db(prn: int, t_s: float, cfg: FadingConfig) -> float:
    import ctypes

    from backend.synth import _lib
    lib = _lib.load_lib()
    _lib.bind_fading(lib)
    c = _lib.FadingCfg(1 if cfg.model == "lognormal" else 0,
                       cfg.sigma_db, cfg.coherence_s, cfg.seed)
    g = float(lib.fading_gain_linear(ctypes.byref(c), prn, float(t_s)))
    return 20.0 * math.log10(max(g, 1e-9))
