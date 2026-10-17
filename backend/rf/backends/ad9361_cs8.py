"""AD9361 backend for Tezuka firmware's TX complex 8-bit (cs8) mode.

The FPGA image built with CS8TX=1 (maia-sdr ``cs8_12_demux`` + the axi_ad9361
patch) treats "only voltage0 of the DDS core enabled" as cs8: every 16-bit DMA
word is ``{Q8, I8}`` (I in the low byte, i.e. the usual .cs8 byte order) and
the FPGA expands it to the DAC's I/Q. That halves the TX link load, so higher
sample rates fit over USB or Ethernet.

Everything except the sample path is shared with backends/ad9361.py. pyadi-iio
cannot enable a single IIO channel (it always enables the I/Q pair of a complex
channel), so the stream goes through libiio directly: only ``voltage0`` is
enabled on the DDS core and the packed bytes are pushed with an ``iio.Buffer``.

Single TX port only (the cs8 path exists for TX1).
"""
from __future__ import annotations

import logging

import numpy as np

from backend.rf.backends import ad9361 as _base
from backend.rf.transmit import TransmitError

_log = logging.getLogger(__name__)

# The stream is quantised to int8 here (the FPGA only left-aligns it), so full
# scale is the int8 one. Symmetric: -128 is never produced.
DAC_FULL_SCALE = 127

_MUTE_GAIN_DB = _base._MUTE_GAIN_DB
_ZERO_FLUSH_SAMPLES = _base._ZERO_FLUSH_SAMPLES

set_gain = _base.set_gain
probe_open = _base.probe_open
probe_close = _base.probe_close
probe_info = _base.probe_info
temperature_c = _base.temperature_c


def n_tx_channels(handle) -> int:
    return 1


def active_channels(handle) -> list[int]:
    return [0]


def set_active_channels(handle, chans) -> None:
    if sorted(set(int(c) for c in chans)) != [0]:
        raise TransmitError("cs8 TX mode drives TX1 only")


def pack_cs8(block) -> bytes:
    """Complex block (already scaled to +-DAC_FULL_SCALE) -> interleaved int8
    I,Q bytes. Rounds and saturates like ad9361._to_dac does for int16."""
    b = np.asarray(block)
    out = np.empty(2 * b.shape[0], dtype=np.int8)
    lim = float(DAC_FULL_SCALE)
    out[0::2] = np.clip(np.rint(b.real), -lim, lim)
    out[1::2] = np.clip(np.rint(b.imag), -lim, lim)
    return out.tobytes()


def open_tx(uri: str, lo_hz: float, sample_rate: float, *,
            rf_bandwidth_hz: float | None = None, xo_ppm: float = 0.0):
    sdr = _base.open_tx(uri, lo_hz, sample_rate,
                        rf_bandwidth_hz=rf_bandwidth_hz, xo_ppm=xo_ppm)
    try:
        _enter_cs8(sdr)
    except Exception:
        try:
            close(sdr)
        except Exception:
            pass
        raise
    return sdr


def _enter_cs8(sdr) -> None:
    dac = getattr(sdr, "_txdac", None)
    if dac is None:
        raise TransmitError("cs8 TX needs a pyadi-iio handle with a DDS core device")
    found = False
    for ch in dac.channels:
        if not getattr(ch, "output", False):
            continue
        is_i0 = str(getattr(ch, "id", "")) == "voltage0"
        ch.enabled = is_i0
        found = found or is_i0
    if not found:
        raise TransmitError("the DDS core exposes no voltage0 output channel")
    sdr._gs_cs8_buf = None
    sdr._gs_cs8_len = 0


def _buffer(handle, n: int):
    buf = getattr(handle, "_gs_cs8_buf", None)
    if buf is not None and handle._gs_cs8_len == n:
        return buf
    import iio  # pylibiio, already a dependency of pyadi-iio
    handle._gs_cs8_buf = None          # drop (and destroy) the old buffer first
    handle._gs_cs8_buf = iio.Buffer(handle._txdac, n, False)
    handle._gs_cs8_len = n
    return handle._gs_cs8_buf


def _push(handle, block) -> None:
    n = len(block)
    buf = _buffer(handle, n)
    buf.write(bytearray(pack_cs8(block)))
    buf.push()


def write(handle, blocks) -> None:
    """``blocks``: one complex array (TX1)."""
    _push(handle, blocks[0])


def close(handle) -> None:
    # Same silence sequence as ad9361.close(): mute, push zeros, release the
    # buffer, power the LOs down. The zero push uses the raw path because
    # handle.tx() would re-enable both I and Q.
    try:
        setattr(handle, "tx_hardwaregain_chan0", _MUTE_GAIN_DB)
    except Exception:
        pass
    try:
        handle.disable_dds()
    except Exception:
        pass
    try:
        _push(handle, np.zeros(_ZERO_FLUSH_SAMPLES, dtype=np.complex64))
    except Exception:
        pass
    handle._gs_cs8_buf = None
    try:
        handle.tx_destroy_buffer()
    except Exception:
        pass
    _base._set_rx_lo_powerdown(handle, True)
    _base._set_tx_lo_powerdown(handle, True)
    _base._set_ensm_mode(handle, "alert")
