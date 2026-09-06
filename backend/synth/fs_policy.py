from __future__ import annotations

import math

from backend.synth.signals import SIGNALS

_STANDARD = (2.6e6, 5.0e6, 10.0e6)


def _weight(sig) -> float:
    return 2.0 if sig.boc is not None else 1.0


def fs_min(signal_ids: list[str]) -> float:
    if not signal_ids:
        raise ValueError("no signals selected")
    return max(SIGNALS[s].chip_rate_hz * _weight(SIGNALS[s]) for s in signal_ids)


def default_fs(signal_ids: list[str]) -> float:
    need = 2.0 * fs_min(signal_ids)
    for f in _STANDARD:
        if f >= need:
            return f
    return math.ceil(need / 1e5) * 1e5


def validate_fs(fs: float | None, signal_ids: list[str]) -> float:
    if fs is None:
        return default_fs(signal_ids)
    fs = float(fs)
    lo = fs_min(signal_ids)
    if fs < lo:
        raise ValueError(
            f"sample_rate {fs:.0f} Hz is below the minimum {lo:.0f} Hz for the "
            f"selected signals {signal_ids}")
    return fs
