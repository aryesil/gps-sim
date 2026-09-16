"""bladeRF backend glue (bladeRF 2.0 micro xA4/xA9 -- built on the same
AD9361/AD9363 transceiver as PlutoSDR, see backend/rf/backends/ad9361.py's
docstring, but driven through Nuand's own libbladeRF/`bladerf` Python
package instead of libiio/pyadi-iio; the two backends' hardware-facing
constraints -- shared LO, shared sample rate across the two TX channels --
are the same chip fact, just reached through a different SDK).

Streaming uses libbladeRF's synchronous interface: one `sync_config()`
call sets up SC16Q11-format, 2-channel-interleaved (TX_X2) transfer once
per card; each `write()` interleaves the two per-channel blocks dual_tx.py's
pump loop already produces into one SC16Q11 buffer (ch0_I, ch0_Q, ch1_I,
ch1_Q per sample period) and pushes it with `sync_tx()`. SC16Q11 samples
are plain int16 in each channel's native -2048..2047 range (Nuand's own
examples scale a unit-magnitude complex tone by 2048.0 before casting) --
the same int16 headroom the on-disk gpssim_*.bin IQ already sits in (see
transmit.TxParams.tx_scale's KNOWN_ISSUES I2 note, peak ~1331 of int16's
+-32767), so no extra scaling is applied here either.

API confirmed against Nuand/bladeRF's own
host/libraries/libbladeRF_bindings/python/bladerf/_bladerf.py; the raw
buffer layout and stream-config values are not verified against real
hardware -- flag any discrepancy found during real-hardware bring-up back
to this module's introducing plan
(docs/superpowers/plans/2026-09-15-bladerf-tx-backend.md) rather than
silently patching around it.

Two deliberate differences from ad9361.py, both because bladeRF's own
model differs from the AD9361/libiio one -- not oversights:

- close() does not mute+zero-flush+destroy_buffer the way ad9361.py's
  does. That sequence works around a libiio/AD9361-DMA quirk (the last
  cyclic TX buffer keeps repeating after a plain buffer-destroy). libbladeRF's
  sync interface is not cyclic-buffer-based; `bladerf_enable_module(dev,
  TX, false)` is libbladeRF's own documented way to turn the RF front end
  off, and disabling both channels (below) is that call. No AD9361-style
  hack is needed or appropriate here.
- The RF-frontend TX quadrature auto-calibration trigger
  (backend/rf/frontend/calibration.py) never fires for this backend --
  capabilities.detect() correctly reports supports_tx_quad_calibration=False,
  since that trigger is a raw libiio `calib_mode` attribute AD9361/pyadi-iio
  boards expose and bladeRF does not. This is not a missing feature: a
  bladeRF's IQ/DC trims (bladerf_get_correction/set_correction --
  DCOFF_I/DCOFF_Q/PHASE/GAIN) live in on-board flash and are loaded
  automatically when the device opens, so there is no "recalibrate after
  retuning the LO" step to trigger in the first place.
"""
from __future__ import annotations

import numpy as np

from backend.rf.transmit import TransmitError

_NUM_BUFFERS = 16
_BUFFER_SIZE = 8192
_NUM_TRANSFERS = 8
_STREAM_TIMEOUT_MS = 3500


def open_tx(uri: str, lo_hz: float, sample_rate: float):
    """Open both TX channels for synchronized 2x2 streaming. Raises
    TransmitError if the device silently clamped the requested LO or
    sample rate on either channel."""
    from bladerf import _bladerf
    dev = _bladerf.BladeRF(device_identifier=uri)
    for idx in (0, 1):
        ch = dev.Channel(_bladerf.CHANNEL_TX(idx))
        ch.frequency = lo_hz
        ch.sample_rate = sample_rate
        ch.bandwidth = sample_rate / 2.0
        if abs(ch.frequency - lo_hz) > 1000:
            raise TransmitError(f"device clamped LO to {ch.frequency}")
        if abs(ch.sample_rate - sample_rate) > 1.0:
            raise TransmitError(f"device clamped rate to {ch.sample_rate}")
    dev.sync_config(layout=_bladerf.ChannelLayout.TX_X2,
                     fmt=_bladerf.Format.SC16_Q11,
                     num_buffers=_NUM_BUFFERS, buffer_size=_BUFFER_SIZE,
                     num_transfers=_NUM_TRANSFERS,
                     stream_timeout=_STREAM_TIMEOUT_MS)
    for idx in (0, 1):
        dev.Channel(_bladerf.CHANNEL_TX(idx)).enable = True
    return dev


def set_gain(handle, chan: int, gain_db: float) -> None:
    from bladerf import _bladerf
    handle.Channel(_bladerf.CHANNEL_TX(chan)).gain = gain_db


def write(handle, blocks) -> None:
    """blocks is [ch0_block, ch1_block], equal-length complex64 arrays --
    interleave into one SC16Q11 buffer (ch0_I, ch0_Q, ch1_I, ch1_Q per
    sample period) and push it."""
    ch0, ch1 = (np.asarray(b, dtype=np.complex64) for b in blocks)
    n = len(ch0)
    interleaved = np.empty(n * 4, dtype=np.int16)
    interleaved[0::4] = ch0.real.astype(np.int16)
    interleaved[1::4] = ch0.imag.astype(np.int16)
    interleaved[2::4] = ch1.real.astype(np.int16)
    interleaved[3::4] = ch1.imag.astype(np.int16)
    handle.sync_tx(interleaved.tobytes(), n)


def close(handle) -> None:
    from bladerf import _bladerf
    try:
        for idx in (0, 1):
            handle.Channel(_bladerf.CHANNEL_TX(idx)).enable = False
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def probe_open(uri: str):
    from bladerf import _bladerf
    return _bladerf.BladeRF(device_identifier=uri)


def probe_info(handle) -> dict:
    """Best-effort hardware identity + temperature -- every field is
    optional, a missing attr must downgrade the readout, never fail the
    connect (mirrors ad9361.probe_info)."""
    info: dict = {}
    try:
        info["hw_serial"] = handle.serial
    except Exception:
        pass
    try:
        info["fw_version"] = str(handle.fpga_version)
    except Exception:
        pass
    try:
        info["temp_c"] = round(float(handle.rfic_temperature), 1)
    except Exception:
        pass
    return info
