"""Tezuka LibreSDR backend that picks the TX sample format from the rate.

The Tezuka FPGA image (CS8TX=1) streams either 16-bit complex samples (the
stock path, ad9361.py) or 8-bit complex samples (ad9361_cs8.py), selected by
which DMA channels the host enables. The libiio network link tops out around
55-60 MB/s, so 16-bit only holds ~14 Msps while 8-bit reaches ~27-30 Msps.

This backend resolves the format once per session from the sample rate:

    rate <= TX_CS16_MAX_RATE_HZ   16-bit complex, both TX ports available
    rate <= TX_CS8_MAX_RATE_HZ    8-bit complex, TX1 only
    rate <= TX_CS4_MAX_RATE_HZ    4-bit complex, TX1 only (needs TX_CS4_ENABLED=1
                                  and a CS4TX=1 FPGA image)
    above                         rejected: the link cannot carry it

16-bit is kept for low rates because it uses the full 12-bit DAC range and
is the most exercised path. The chosen implementation is stored on the handle
and every other call is delegated to it, so the rest of the stack sees one
ordinary backend.
"""
from __future__ import annotations

from backend import config
from backend.rf.backends import ad9361, ad9361_cs4, ad9361_cs8
from backend.rf.transmit import TransmitError

# Device-level helpers do not depend on the format.
probe_open = ad9361.probe_open
probe_close = ad9361.probe_close
probe_info = ad9361.probe_info
set_gain = ad9361.set_gain

# Module-level fallback; the per-session value comes from dac_full_scale_for().
DAC_FULL_SCALE = ad9361.DAC_FULL_SCALE


def impl_for(sample_rate: float):
    """The format implementation (ad9361 = cs16, ad9361_cs8, ad9361_cs4) for a rate."""
    rate = float(sample_rate)
    if rate <= config.TX_CS16_MAX_RATE_HZ:
        return ad9361
    if rate <= config.TX_CS8_MAX_RATE_HZ:
        return ad9361_cs8
    if config.TX_CS4_ENABLED and rate <= config.TX_CS4_MAX_RATE_HZ:
        return ad9361_cs4
    top = config.TX_CS4_MAX_RATE_HZ if config.TX_CS4_ENABLED else config.TX_CS8_MAX_RATE_HZ
    hint = ("" if config.TX_CS4_ENABLED else
            "; 4-bit samples reach further with a CS4TX=1 FPGA image and TX_CS4_ENABLED=1")
    raise TransmitError(
        f"sample rate {rate / 1e6:g} Msps is above the {top / 1e6:g} "
        f"Msps the TX link can carry (the libiio network link sustains ~55-60 MB/s){hint}")


def format_for(sample_rate: float) -> str:
    impl = impl_for(sample_rate)
    return "cs4" if impl is ad9361_cs4 else "cs8" if impl is ad9361_cs8 else "cs16"


def dac_full_scale_for(sample_rate: float) -> float:
    return float(impl_for(sample_rate).DAC_FULL_SCALE)


def level_offset_db_for(sample_rate: float) -> float:
    """dB added to the requested level (see ad9361_cs4.LEVEL_OFFSET_DB)."""
    return float(getattr(impl_for(sample_rate), "LEVEL_OFFSET_DB", 0.0))


def _impl(handle):
    return handle._gs_impl


def open_tx(uri: str, lo_hz: float, sample_rate: float, *,
            rf_bandwidth_hz: float | None = None, xo_ppm: float = 0.0):
    impl = impl_for(sample_rate)
    handle = impl.open_tx(uri, lo_hz, sample_rate,
                          rf_bandwidth_hz=rf_bandwidth_hz, xo_ppm=xo_ppm)
    handle._gs_impl = impl
    return handle


def n_tx_channels(handle) -> int:
    return _impl(handle).n_tx_channels(handle)


def active_channels(handle) -> list[int]:
    return _impl(handle).active_channels(handle)


def set_active_channels(handle, chans) -> None:
    _impl(handle).set_active_channels(handle, chans)


def write(handle, blocks) -> None:
    _impl(handle).write(handle, blocks)


def air_format_for(sample_rate: float):
    """Packed format (backend.rf.air) the rate will use, None for cs16."""
    getter = getattr(impl_for(sample_rate), "air_format_for", None)
    return getter(sample_rate) if getter is not None else None


def air_bytes_per_sample(handle) -> int:
    return int(_impl(handle).AIR_BYTES_PER_SAMPLE)


def write_air(handle, blocks) -> None:
    _impl(handle).write_air(handle, blocks)


def close(handle) -> None:
    _impl(handle).close(handle)


def temperature_c(handle):
    return _impl(handle).temperature_c(handle)
