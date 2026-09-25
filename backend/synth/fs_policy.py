from __future__ import annotations

import math

from backend.synth.signals import SIGNALS

_STANDARD = (2.6e6, 5.0e6, 10.0e6)


def _weight(sig) -> float:
    return 2.0 if sig.boc is not None else 1.0


def carrier_span_hz(signal_ids: list[str]) -> float:
    """Distance between the lowest and highest carrier in one output band
    (non-zero only for BeiDou B1I sharing the L1 output with GPS/Galileo)."""
    cs = [SIGNALS[s].carrier_hz for s in signal_ids]
    return max(cs) - min(cs) if cs else 0.0


def _spanned_need(signal_ids: list[str]) -> float:
    """Complex fs holding every carrier's main lobe: the carrier span plus a
    main-lobe half-width (chip rate, x2 for BOC) on each side."""
    lobe = max(SIGNALS[s].chip_rate_hz * _weight(SIGNALS[s]) for s in signal_ids)
    return math.ceil((carrier_span_hz(signal_ids) + 2.0 * lobe) / 1e5) * 1e5


def fs_min(signal_ids: list[str], *, channel_span_hz: float = 0.0) -> float:
    if not signal_ids:
        raise ValueError("no signals selected")
    chip_rate_floor = max(SIGNALS[s].chip_rate_hz * _weight(SIGNALS[s]) for s in signal_ids)
    if carrier_span_hz(signal_ids) > 0.0:
        chip_rate_floor = max(chip_rate_floor, _spanned_need(signal_ids))
    return max(chip_rate_floor, channel_span_hz)


def default_fs(signal_ids: list[str]) -> float:
    if carrier_span_hz(signal_ids) > 0.0:
        return max(20.0e6, _spanned_need(signal_ids))
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
    elif band_id == "G2":
        if not ks:
            ks = range(-7, 7)
        max_abs_k = max(abs(k) for k in ks)
        max_chip_rate = max(SIGNALS[s].chip_rate_hz for s in signal_ids)
        channel_span = 2 * (max_abs_k * 437_500.0 + max_chip_rate)
        need = max(fs_min(signal_ids), channel_span)
        for f in _STANDARD:
            if f >= need:
                return f
        return math.ceil(need / 1e5) * 1e5
    elif band_id == "L2":
        # GPS L2C main lobe +/- 1.023 MHz; GLONASS L2 rides the G2 band, so
        # L2 here is the GPS-only group.
        need = 2.0 * fs_min(signal_ids)
        for f in _STANDARD:
            if f >= need:
                return f
        return math.ceil(need / 1e5) * 1e5
    elif band_id == "L5":
        # 10.23 Mcps BPSK(10): main lobe +/- 10.23 MHz. Round up to a
        # 0.1 MHz grid above 2x the chip rate.
        max_chip = max(SIGNALS[s].chip_rate_hz for s in signal_ids)
        need = max(2.0 * max_chip, fs_min(signal_ids))
        return math.ceil(need / 1e5) * 1e5
    else:
        raise ValueError(f"unknown band {band_id!r}")


def validate_fs(fs: float | None, signal_ids: list[str]) -> float:
    if fs is None:
        return default_fs(signal_ids)
    fs = float(fs)
    lo = fs_min(signal_ids)
    if fs < lo:
        extra = ""
        if carrier_span_hz(signal_ids) > 0.0:
            extra = (" -- BeiDou B1I (1561.098 MHz) and GPS/Galileo L1 "
                     "(1575.42 MHz) share one output, so it must span both "
                     "carriers; raise the sample rate or drop BeiDou")
        raise ValueError(
            f"sample_rate {fs:.0f} Hz is below the minimum {lo:.0f} Hz for the "
            f"selected signals {signal_ids}{extra}")
    return fs
