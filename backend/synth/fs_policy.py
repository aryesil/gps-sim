from __future__ import annotations

import math

from backend.synth.signals import SIGNALS

_STANDARD = (2.6e6, 5.0e6, 10.0e6)


def _weight(sig) -> float:
    return 2.0 if sig.boc is not None else 1.0


def fs_min(signal_ids: list[str], *, channel_span_hz: float = 0.0) -> float:
    if not signal_ids:
        raise ValueError("no signals selected")
    chip_rate_floor = max(SIGNALS[s].chip_rate_hz * _weight(SIGNALS[s]) for s in signal_ids)
    return max(chip_rate_floor, channel_span_hz)


def default_fs(signal_ids: list[str]) -> float:
    need = 2.0 * fs_min(signal_ids)
    for f in _STANDARD:
        if f >= need:
            return f
    return math.ceil(need / 1e5) * 1e5


def band_floor(band_id: str, signal_ids: list[str], ks=()) -> float:
    """Compute the minimum sample rate floor for a band, accounting for FDMA span.

    Args:
        band_id: Band identifier ("L1", "G1", etc.)
        signal_ids: List of signal keys in SIGNALS dict
        ks: Iterable of FDMA channel indices (for "G1"), defaults to [-7, 6] if empty

    Returns:
        Minimum sample rate in Hz

    Raises:
        ValueError: If band_id is unknown
    """
    if band_id == "L1":
        return fs_min(signal_ids)
    elif band_id == "G1":
        # Compute FDMA span requirement
        if not ks:
            ks = range(-7, 7)
        max_abs_k = max(abs(k) for k in ks)
        max_chip_rate = max(SIGNALS[s].chip_rate_hz for s in signal_ids)
        channel_span = 2 * (max_abs_k * 562_500.0 + max_chip_rate)

        # Get the chip-rate floor
        chip_rate_floor = fs_min(signal_ids)

        # Take the max and round up to standard rate
        need = max(chip_rate_floor, channel_span)
        for f in _STANDARD:
            if f >= need:
                return f
        return math.ceil(need / 1e5) * 1e5
    else:
        raise ValueError(f"unknown band {band_id!r}")


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
